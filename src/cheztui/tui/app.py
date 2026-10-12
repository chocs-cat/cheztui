"""The Textual app: header, view tabs, log line and key bar (spec §4, §5, §10).

The views fill the list and detail panes between them. Reads run in a
worker (DEC-6): the version check once it passes, then the Drift read and
the header's read at once. A failure clears what that read feeds, and its
diagnostic is masked before the log line or the session log sees it
(DEC-30, DEC-33).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
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
from cheztui.runner import Runner, run_subprocess
from cheztui.secrets import Masker
from cheztui.tui import keys
from cheztui.tui.screens import HelpScreen, Palette, SessionLogScreen, help_text
from cheztui.tui.settings import SettingsScreen, launch_editor
from cheztui.tui.views.base import Detail, ListDetail, RowList
from cheztui.tui.views.drift import DriftView

THEMES = frozenset(BUILTIN_THEMES)  # the themes `ui.theme` may name (spec §9)
READING = "reading chezmoi…"


def _action_name(action: str) -> str:
    return action.split("(", 1)[0]


_FRAME_ACTIONS = frozenset(_action_name(a) for k in keys.GLOBAL for _, a in k.bindings)
CANCELLED = "refresh cancelled"


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
        self.session_log: list[LogEntry] = []
        self.repo: ops.RepoState | None = None
        self.reading = False
        self._version_ok = False
        self._read_id = 0

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

    def _render_key_bar(self) -> None:
        parts = [
            Content.assemble((key.keys, "bold $accent"), f" {key.label}")
            for key in self.key_bar_keys()
        ]
        self.query_one("#key-bar", Static).update(_joined(parts, "  "))

    # --- Log line and session log ---------------------------------------------

    def set_log_line(self, text: str, *, error: bool = False) -> None:
        line = Content.styled(f"✗ {text}", "$error") if error else Content(text)
        self.query_one("#log-line", Static).update(line)

    def record(self, text: str, diagnostic: str | None = None) -> None:
        """Add a result, error or cancellation to the session log (spec §5)."""
        self.session_log.append(LogEntry(self.now(), text, diagnostic))

    # --- Reads (spec §5 Reads in the TUI; DEC-33) -------------------------------

    def action_refresh(self) -> None:
        self.start_read()

    def action_view(self, name: str) -> None:
        """Switch view; Drift is the only one so far (DEC-20)."""

    def start_read(self) -> None:
        """Read chezmoi; a read still running is cancelled by this one."""
        self._read_id += 1
        self.reading = True
        self.set_log_line(READING)
        self.run_worker(self._read(self._read_id), group="read", exclusive=True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The frame's keys work on the main screen only; help, the palette, the
        # session log and settings have their own (spec §5).
        if action in _FRAME_ACTIONS and self.screen is not self.screen_stack[0]:
            return False
        return self.available(action)

    def available(self, action: str) -> bool:
        """Whether a frame action works now, whatever screen is on top."""
        if action == "cancel_read":
            return self.reading
        if action in ("filter", "select"):
            return self.view.has_rows
        if action == "toggle_focus":
            return self.narrow
        return True

    @property
    def palette_keys(self) -> list[keys.Key]:
        """The palette's entries: the view's, then the global ones, that work now (spec §5)."""
        listed = [*self.view.KEYS, *keys.GLOBAL]
        return [k for k in listed if k.palette and self.available(_action_name(k.action))]

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

    def action_cancel_read(self) -> None:
        """`esc`: cancel the read; the earlier data stays (spec §5)."""
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
            failures.append((message, stderr.rstrip("\n") or message))

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
        self._render_frame()
        if read_id != self._read_id:
            return
        self.reading = False
        for message, diagnostic in failures:
            self.record(f"✗ {message}", diagnostic)
        if failures:
            self.set_log_line(failures[0][0], error=True)
        else:
            self.set_log_line("")
        self._render_key_bar()


def _joined(parts: list[Content], separator: str) -> Content:
    pieces: list[Content | str] = []
    for i, part in enumerate(parts):
        pieces += [separator, part] if i else [part]
    return Content.assemble(*pieces)
