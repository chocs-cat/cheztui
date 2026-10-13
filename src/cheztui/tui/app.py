"""The Textual app: header, view tabs, log line and key bar (spec §4, §5, §10).

The views fill the list and detail panes between them. Reads run in a
worker (DEC-6): the version check once it passes, then the Drift read and
the header's read at once. A failure clears what that read feeds, and its
diagnostic is masked before the log line or the session log sees it
(DEC-30, DEC-33).

Writes run in one worker of their own (spec §6 Drift, *Actions in the
TUI*; DEC-35): prepare through the app's one `ops.WriteOps`, show the
preview modal when the plan needs confirming or is blocked, execute with
the terminal runner for edit and merge, log the result, clear the
selection and refresh. What a plan holds and how a result is worded are
`ops.py`'s; the app lays out, suspends and logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.containers import Horizontal
from textual.content import Content
from textual.theme import BUILTIN_THEMES
from textual.widgets import Static

from cheztui import ops
from cheztui.chezmoi import Chezmoi, ChezmoiError, ChezmoiFailed
from cheztui.config import Config, ConfigError, load
from cheztui.git import GitError, GitFailed
from cheztui.runner import Completed, Runner, run_subprocess
from cheztui.secrets import Masker
from cheztui.tui import keys
from cheztui.tui.screens import HelpScreen, Palette, SessionLogScreen, help_text
from cheztui.tui.settings import SettingsScreen, launch_editor
from cheztui.tui.views.base import Detail, ListDetail, RowList
from cheztui.tui.views.drift import DriftView
from cheztui.tui.widgets.confirm import PreviewModal

THEMES = frozenset(BUILTIN_THEMES)  # the themes `ui.theme` may name (spec §9)
READING = "reading chezmoi…"
BLOCKED = "the source tree has uncommitted changes"


def _action_name(action: str) -> str:
    return action.split("(", 1)[0]


_FRAME_ACTIONS = frozenset(_action_name(a) for k in keys.GLOBAL for _, a in k.bindings)
CANCELLED = "refresh cancelled"
PREPARING = "preparing…"
RUNNING = "running…"
PREVIEW_CANCELLED = "preview cancelled"


@dataclass(frozen=True)
class LogEntry:
    """One session-log entry (spec §5): its log-line text, and an error's whole diagnostic."""

    time: datetime
    text: str
    diagnostic: str | None = None


class CheztuiApp(App[None]):
    TITLE = "cheztui"

    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; padding: 0 1; }
    #header-left { width: 1fr; }
    #header-right { width: auto; }
    #tabs { height: 1; padding: 0 1; }
    DriftView { height: 1fr; }
    #panes {
        layout: horizontal;
        height: 1fr;
        border-top: solid $panel;
        border-bottom: solid $panel;
    }
    #panes.-narrow { layout: vertical; }
    #list-pane { width: 2fr; height: 1fr; }
    #detail { width: 3fr; height: 1fr; border-left: solid $panel; padding: 0 1; }
    #detail-rule { margin: 0; color: $panel; }
    #panes.-narrow #list-pane { width: 1fr; height: 1fr; }
    #panes.-narrow #detail {
        width: 1fr;
        height: 1fr;
        border-left: none;
        border-top: solid $panel;
    }
    RowList, RowList:focus { border: none; padding: 0; height: 1fr; background: $background; }
    #detail { background: $background; }
    #list-message { padding: 0 1; height: auto; display: none; }
    .-no-rows RowList { display: none; }
    .-no-rows #list-message { display: block; }
    #filter { display: none; }
    #log-line { height: 1; padding: 0 1; }
    #key-bar { height: 1; padding: 0 1; }
    """

    BINDINGS = keys.bindings(keys.GLOBAL)

    def __init__(
        self, config: Config, chezmoi: Chezmoi, *, git_runner: Runner = run_subprocess
    ) -> None:
        super().__init__()
        self.settings = config
        self.chezmoi = chezmoi
        self.git_runner = git_runner
        self.theme = config.ui.theme
        self.now: Callable[[], datetime] = datetime.now  # the session log's clock
        self.launch_editor: Callable[[Path], None] = launch_editor  # tests replace it
        # Edit and merge run here, the terminal handed over (spec §10); tests replace it.
        self.terminal_runner: Runner = self.run_in_terminal
        # One for the app: its lock serializes cheztui's writes (DEC-27).
        self.writes = ops.WriteOps(chezmoi, config, git_runner=git_runner)
        self.session_log: list[LogEntry] = []
        self.repo: ops.RepoState | None = None
        self.reading = False
        self.writing: str | None = None  # PREPARING or RUNNING while a write is under way
        self._version_ok = False
        self._read_id = 0
        self._write_id = 0
        self._keep_line = False  # the refresh after a write leaves its result showing

    # --- Layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="header-left", markup=False)
            yield Static(id="header-right")
        yield Static(id="tabs")
        yield DriftView(id="drift")
        yield Static(id="log-line")
        yield Static(id="key-bar")

    @property
    def view(self) -> ListDetail:
        return self.query_one(DriftView)

    def on_mount(self) -> None:
        self.update_layout()
        self._render_frame()
        self.view.query_one("#list").focus()
        self.start_read()

    def on_resize(self, event: events.Resize) -> None:
        self.update_layout(event.size.width)

    def update_layout(self, width: int | None = None) -> None:
        """Stack the panes below `ui.narrow_breakpoint` columns (spec §4 Panes)."""
        width = self.size.width if width is None else width
        narrow = width < self.settings.ui.narrow_breakpoint
        for panes in self.query("#panes"):
            panes.set_class(narrow, "-narrow")
        # `tab` works only when stacked, so only then can the detail hold focus (spec §4).
        for detail in self.query(Detail):
            detail.can_focus = narrow
            if not narrow and detail.has_focus:
                self.view.query_one(RowList).focus()

    @property
    def narrow(self) -> bool:
        return self.query_one("#panes").has_class("-narrow")

    def _render_frame(self) -> None:
        self._render_header()
        self._render_tabs()
        self._render_key_bar()

    def _render_header(self) -> None:
        """Source on the left; verify, branch, cleanliness and `DRY RUN` on the right (spec §4)."""
        repo = self.repo
        left = "cheztui" + (f" • {ops.display_target(repo.source_dir)}" if repo else "")
        self.query_one("#header-left", Static).update(left)
        fields: list[Content] = []
        drift_rows = self.query_one(DriftView).rows
        if drift_rows is not None:
            fields.append(
                Content.styled("✗ verify", "$error")
                if drift_rows
                else Content.styled("✓ verify", "$success")
            )
        if repo is not None:
            if repo.branch is None:
                fields.append(Content("detached"))
            elif repo.ahead_behind is None:
                fields.append(Content(repo.branch))
            else:
                ahead, behind = repo.ahead_behind
                fields.append(Content(f"{repo.branch} ↑{ahead} ↓{behind}"))
            fields.append(
                Content(f"{repo.uncommitted} uncommitted" if repo.uncommitted else "clean")
            )
        if self.chezmoi.dry_run:
            fields.append(Content.styled("DRY RUN", "bold $warning"))
        self.query_one("#header-right", Static).update(_joined(fields, "   "))

    def _render_tabs(self) -> None:
        """Only views that exist get a tab (DEC-20); the count is the view's rows (spec §4)."""
        view = self.view
        label = f" 1 {view.NAME}" + (f" {view.count}" if view.count else "") + " "
        self.query_one("#tabs", Static).update(Content.styled(label, "reverse"))

    def key_bar_keys(self) -> list[keys.Key]:
        """The view's keys that work now, then the global ones in bar order (spec §4 Key bar)."""
        listed = [*self.view.KEYS, *sorted((k for k in keys.GLOBAL if k.bar), key=lambda k: k.bar)]
        return [k for k in listed if k.label and self._works(k)]

    def _works(self, key: keys.Key) -> bool:
        return self.check_action(_action_name(key.action), ()) is not False

    def key_label(self, key: keys.Key) -> str:
        """A key's word; a view's key may change it with the view's state (`t`, spec §6)."""
        return self.view.key_label(key) if key in self.view.KEYS else key.label

    def _render_key_bar(self) -> None:
        parts = [
            Content.assemble((key.keys, "bold $accent"), f" {self.key_label(key)}")
            for key in self.key_bar_keys()
        ]
        self.query_one("#key-bar", Static).update(_joined(parts, "  "))

    def on_list_detail_changed(self, event: ListDetail.Changed) -> None:
        self._render_key_bar()

    # --- Log line and session log ---------------------------------------------

    def set_log_line(self, text: str, *, error: bool = False, mark: bool = True) -> None:
        """Show `text`; an error is `✗ <text>` in the error colour, or without the
        `✗` (`mark=False`) for a line whose parts carry their own (spec §6 *Result*)."""
        shown = f"✗ {text}" if error and mark else text
        line = Content.styled(shown, "$error") if error else Content(shown)
        self.query_one("#log-line", Static).update(line)

    def record(self, text: str, diagnostic: str | None = None) -> None:
        """Add a result, error or cancellation to the session log (spec §5)."""
        self.session_log.append(LogEntry(self.now(), text, diagnostic))

    # --- Reads (spec §5 Reads in the TUI; DEC-33) -------------------------------

    def action_refresh(self) -> None:
        self.start_read()

    def action_view(self, name: str) -> None:
        """Switch view; Drift is the only one so far (DEC-20)."""

    def start_read(self, *, after_write: bool = False) -> None:
        """Read chezmoi; a read still running is cancelled by this one.

        The detail's bodies are read again too (spec §6 Detail reads). The
        refresh after a write leaves the write's result on the log line,
        unless it fails or is cancelled (spec §4 Log line, DEC-35).
        """
        self._read_id += 1
        self.reading = True
        self._keep_line = after_write
        if not after_write:
            self.set_log_line(READING)
        self.view.reset_detail()
        self.run_worker(self._read(self._read_id), group="read", exclusive=True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The frame's keys work on the main screen only; help, the palette, the
        # session log and settings have their own (spec §5).
        if action in _FRAME_ACTIONS and self.screen is not self.screen_stack[0]:
            return False
        if action in self._view_actions:
            # A view's key, asked about by the key bar.
            return self.view.check_action(action, parameters)
        return self.available(action)

    @property
    def _view_actions(self) -> frozenset[str]:
        return frozenset(_action_name(k.action) for k in self.view.KEYS)

    def available(self, action: str) -> bool:
        """Whether a frame action works now, whatever screen is on top."""
        if action == "cancel_read":
            return self.reading or self.writing == PREPARING
        if action == "refresh":
            return self.writing is None  # the write's own refresh follows (DEC-35)
        if action in ("filter", "select"):
            return self.view.has_rows
        if action == "toggle_focus":
            return self.narrow
        if action == "reveal":
            return self.view.can_reveal
        return True

    @property
    def palette_keys(self) -> list[keys.Key]:
        """The palette's entries: the view's, then the global ones, that work now (spec §5)."""
        view = [k for k in self.view.KEYS if k.palette and self._works(k)]
        frame = [k for k in keys.GLOBAL if k.palette and self.available(_action_name(k.action))]
        return view + frame

    async def run_key(self, key: keys.Key) -> None:
        """Run a key's action as pressing it would, a view's key on its view (spec §5)."""
        await self.run_action(key.action, self.view if key in self.view.KEYS else None)

    # --- Help, palette, session log, settings (spec §5, §9) ------------------------

    def action_help(self) -> None:
        view = self.view
        self.push_screen(HelpScreen(help_text(view.NAME, view.KEYS, keys.GLOBAL)))

    def action_command_palette(self) -> None:
        if not isinstance(self.screen, Palette):
            self.push_screen(Palette())

    def action_session_log(self) -> None:
        self.push_screen(SessionLogScreen(self.session_log))

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def apply_settings(self) -> None:
        """Make the theme and the layout follow the settings at once (spec §9)."""
        self.theme = self.settings.ui.theme
        self.writes.config = self.settings  # the write path's masking reads it
        self.update_layout()

    def edit_config(self) -> str | None:
        """Edit the config file suspended, then reload it; a config error keeps the old one.

        Returns the error's message, or None when the file loaded.
        """
        path = self.settings.path
        with contextlib.ExitStack() as stack:
            # A headless test run can't suspend; the tests replace the launcher.
            with contextlib.suppress(SuspendNotSupported):
                stack.enter_context(self.suspend())
            self.launch_editor(path)
        try:
            self.settings = load(path)
        except ConfigError as e:
            return str(e)
        self.apply_settings()
        return None

    # --- The list's keys (spec §4 List, §5 Keys) ---------------------------------

    @property
    def detail(self) -> Detail:
        return self.view.query_one(Detail)

    def action_move(self, step: int) -> None:
        """`j`/`k`: move the current row, or scroll the detail when it has focus."""
        if self.detail.has_focus:
            self.detail.scroll_relative(y=step, animate=False)
        else:
            self.view.move(step)

    def action_scroll_detail(self, step: int) -> None:
        self.detail.scroll_relative(y=step, animate=False)

    def action_page_detail(self, step: int) -> None:
        if step > 0:
            self.detail.scroll_page_down(animate=False)
        else:
            self.detail.scroll_page_up(animate=False)

    def action_filter(self) -> None:
        self.view.open_filter()

    def action_select(self) -> None:
        self.view.toggle_selected()

    def action_toggle_focus(self) -> None:
        if self.detail.has_focus:
            self.view.query_one(RowList).focus()
        else:
            self.detail.focus()

    def action_reveal(self) -> None:
        """`R`: reveal the body on screen, or mask it again (spec §6 Reveal)."""
        self.view.toggle_reveal()
        self._render_key_bar()

    def action_cancel_read(self) -> None:
        """`esc`: cancel a preview being prepared, else the read; the earlier data
        stays (spec §5, §6 *Prepare*)."""
        if self.writing == PREPARING:
            self._cancel_write()
            return
        if not self.reading:
            return
        self.workers.cancel_group(self, "read")
        self._read_id += 1  # whatever the cancelled read was doing, it no longer reports
        self.reading = False
        self.set_log_line(CANCELLED)
        self.record(CANCELLED)

    async def _read(self, read_id: int) -> None:
        masker = Masker(self.settings.secrets, Path("/"))
        failures: list[tuple[str, str]] = []

        def fail(error: ChezmoiError | GitError) -> None:
            # The error is already sanitized; masking its message too covers
            # parser messages that quote chezmoi's output (DEC-33).
            message = ops.mask_diagnostic(masker, ops.failure_message(error), whole=False)
            stderr = error.stderr if isinstance(error, ChezmoiFailed | GitFailed) else ""
            diagnostic = stderr.rstrip("\n") or message
            failures.append((message, diagnostic))
            # Logged as it is shown: a read cancelled before it ends must not
            # drop an error already on screen (spec §5, #31).
            self.record(f"✗ {message}", diagnostic)

        if not self._version_ok:
            try:
                await ops.sanitized(self.chezmoi.check_version(), masker, whole=False)
            except ChezmoiError as e:
                fail(e)
                self.repo = None
                self.view.show_message(None)
                self._finish(read_id, failures)
                return
            self._version_ok = True

        async def read_drift() -> None:
            try:
                drift = await ops.sanitized(ops.drift(self.chezmoi), masker, whole=False)
            except (ChezmoiError, GitError) as e:
                fail(e)
                self.view.show_message(f"✗ {failures[-1][0]}")
            else:
                self.view.show_rows(drift.rows)
            self._render_frame()

        async def read_repo() -> None:
            try:
                self.repo = await ops.sanitized(
                    ops.repo_state(self.chezmoi, git_runner=self.git_runner),
                    masker,
                    whole=False,
                )
            except (ChezmoiError, GitError) as e:
                fail(e)
                self.repo = None
            self._render_header()

        await asyncio.gather(read_drift(), read_repo())
        self._finish(read_id, failures)

    def _finish(self, read_id: int, failures: list[tuple[str, str]]) -> None:
        """End the read: the log line shows its first failure, already logged."""
        self._render_frame()
        if read_id != self._read_id:
            return
        self.reading = False
        if failures:
            self.set_log_line(failures[0][0], error=True)
        elif not self._keep_line:
            self.set_log_line("")
        self._keep_line = False
        self._render_key_bar()

    # --- Writes (spec §6 Drift, *Actions in the TUI*; DEC-35) ---------------------

    def start_write(self, prepare: Callable[[], Awaitable[ops.WritePlan]]) -> None:
        """Prepare a plan (`prepare` holds the rows taken when the key was pressed),
        preview it, run it, report it."""
        if self.writing is not None:
            return
        self._write_id += 1
        self.writing = PREPARING
        self.set_log_line(PREPARING)
        self._render_key_bar()
        self.run_worker(self._write(prepare, self._write_id), group="write", exclusive=True)

    def _cancel_write(self) -> None:
        """`esc` while preparing: nothing has written yet (spec §5)."""
        self.workers.cancel_group(self, "write")
        self._write_id += 1
        self._end_write()
        self.set_log_line(PREVIEW_CANCELLED)
        self.record(PREVIEW_CANCELLED)

    def _end_write(self) -> None:
        self.writing = None
        self._render_key_bar()

    async def _write(self, prepare: Callable[[], Awaitable[ops.WritePlan]], write_id: int) -> None:
        # Diagnostics that can't be assigned to a target: line level, no never_mask (DEC-30).
        masker = Masker(self.settings.secrets, Path("/"))
        try:
            plan = await ops.sanitized(prepare(), masker, whole=False)
        except (ChezmoiError, GitError) as e:
            if write_id != self._write_id:
                return
            message = ops.mask_diagnostic(masker, ops.failure_message(e), whole=False)
            stderr = e.stderr if isinstance(e, ChezmoiFailed | GitFailed) else ""
            self.set_log_line(message, error=True)
            self.record(f"✗ {message}", stderr.rstrip("\n") or message)
            self._end_write()
            return
        if write_id != self._write_id:
            return
        context = self.settings.ui.diff_context
        if plan.blocked:
            # Logged as the modal opens, so it outlives the modal (DEC-35).
            if plan.preflight.blocked:
                line, diagnostic = BLOCKED, "\n".join(plan.preflight.stray)
            else:
                line, diagnostic = f"preview failed: {plan.error}", plan.diagnostic
            self.set_log_line(line, error=True)
            self.record(f"✗ {line}", diagnostic.rstrip("\n") or None)
            await self.push_screen_wait(PreviewModal(plan, context=context))
            self._end_write()
            return
        if plan.needs_confirmation:
            self.set_log_line("")  # the preparation has ended; nothing has run (DEC-36)
            chosen = await self.push_screen_wait(PreviewModal(plan, context=context))
            if chosen is None:
                self.set_log_line(PREVIEW_CANCELLED)
                self.record(PREVIEW_CANCELLED)
                self._end_write()
                return
            plan = chosen
        else:
            # An interactive plan opens no modal (DEC-4): its left-out rows are logged.
            for entry in plan.entries:
                if entry.excluded is not None:
                    self.record(ops.left_out_line(entry))
        self.writing = RUNNING
        self.set_log_line(RUNNING)
        result = await self.writes.execute(
            plan, confirmed=True, interactive_runner=self.terminal_runner
        )
        shown = ops.report(result)
        for text, diagnostic in shown.entries:
            self.record(text, diagnostic)
        self.set_log_line(shown.line, error=shown.failed, mark=False)
        # After every execution: the selection goes and the view refreshes (DEC-35).
        self.view.clear_selection()
        self._end_write()
        self.start_read(after_write=True)

    def full_diff(self, target: Path) -> None:
        """`d`: chezmoi's own diff in the terminal, its pager and diff command applying.

        A read, not a write: no pre-flight, and it runs under `-n` (spec §6 `d`).
        """
        self.run_worker(self._full_diff(target), group="terminal")

    async def _full_diff(self, target: Path) -> None:
        done = await self.chezmoi.run("diff", str(target), check=False, runner=self.terminal_runner)
        if done.returncode != 0:
            line = f"chezmoi diff exited {done.returncode}"
            self.set_log_line(line, error=True)
            self.record(f"✗ {line}")

    async def run_in_terminal(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed:
        """The terminal runner: the command with the terminal under `App.suspend()`,
        `env` merged over the environment; its exit code, and no output (spec §10)."""
        with contextlib.ExitStack() as stack:
            # A headless test run can't suspend; the tests replace this runner.
            with contextlib.suppress(SuspendNotSupported):
                stack.enter_context(self.suspend())
            done = subprocess.run(
                list(argv), cwd=cwd, env={**os.environ, **(env or {})}, check=False
            )
        return Completed(tuple(argv), done.returncode, b"", b"")


def _joined(parts: list[Content], separator: str) -> Content:
    pieces: list[Content | str] = []
    for i, part in enumerate(parts):
        pieces += [separator, part] if i else [part]
    return Content.assemble(*pieces)
