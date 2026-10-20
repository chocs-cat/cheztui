"""The Drift view's actions (spec §6 Drift, *Actions in the TUI*; DEC-4, DEC-35).

Every test but the ones marked `requires_chezmoi` runs the app on
`FakeChezmoi.drifted(root)`, whose home has real files, with the terminal
runner replaced by a recording fake: a headless run can't suspend. The live
ones run the real chezmoi in the scratch setup and don't reach the terminal
runner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from textual.pilot import Pilot

from cheztui.config import Config
from cheztui.runner import Completed
from cheztui.secrets import MASK
from cheztui.tui.app import CheztuiApp
from cheztui.tui.screens import HelpScreen, SessionLogScreen
from cheztui.tui.views.drift import DriftView
from cheztui.tui.widgets.confirm import PreviewModal
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_chezmoi import FakeChezmoi
from tests.fake_write import subcommand
from tests.test_chezmoi_reads import build_drift
from tests.test_tui import TOKEN, make_app, styles, text
from tests.test_tui import settled as workers_done
from tests.test_tui_confirm import footer
from tests.test_tui_drift import move_to
from tests.test_tui_screens import palette_entries


class Terminal:
    """A terminal runner that records each command and exits with `code`."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed:
        self.calls.append(tuple(argv))
        return Completed(tuple(argv), self.code, b"", b"")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "scratch" / "home"))
    return tmp_path / "scratch"


def drift_app(root: Path, tmp_path: Path, **kwargs) -> tuple[CheztuiApp, FakeChezmoi, Terminal]:
    flags = {k: kwargs.pop(k) for k in ("dry_run",) if k in kwargs}
    fake = FakeChezmoi.drifted(root, **kwargs)
    app = make_app(fake, tmp_path, **flags)
    terminal = Terminal()
    app.terminal_runner = terminal
    return app, fake, terminal


def view(app: CheztuiApp) -> DriftView:
    return app.query_one(DriftView)


def key_bar(app: CheztuiApp) -> str:
    return text(app, "#key-bar")


def log_line(app: CheztuiApp) -> str:
    return text(app, "#log-line")


def logged(app: CheztuiApp) -> list[str]:
    return [e.text for e in app.session_log]


def writes(fake: FakeChezmoi) -> list[tuple[str, ...]]:
    """The writing chezmoi calls that ran for real: not the previews' dry runs."""
    return [w for w in fake.writes if "--dry-run" not in w]


def statuses(fake: FakeChezmoi) -> int:
    return sum(1 for c in fake.chezmoi_calls if c[:1] == ("status",))


async def settled(pilot: Pilot) -> None:
    """Wait for the write and the refresh it starts: a worker started by another
    after `wait_for_complete` was called isn't waited for."""
    app = pilot.app
    assert isinstance(app, CheztuiApp)
    for _ in range(200):
        await workers_done(pilot)
        if not app.reading and app.writing is None:
            return
    raise AssertionError("still reading or writing")


async def until(pilot: Pilot, check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            return
        await pilot.pause()
    raise AssertionError("never happened")


async def until_modal(pilot: Pilot) -> PreviewModal:
    await until(pilot, lambda: isinstance(pilot.app.screen, PreviewModal))
    screen = pilot.app.screen
    assert isinstance(screen, PreviewModal)
    return screen


async def select(pilot: Pilot, *names: str) -> None:
    for name in names:
        await move_to(pilot, name)
        await pilot.press("space")


# --- Keys (spec §6 *Actions in the TUI*, Keys) -----------------------------------------

TAIL = "  space Select  / Filter  ? Help"


@pytest.mark.parametrize(
    ("name", "bar"),
    [
        (".mine", "enter Fix  a Apply  r Re-add  m Merge  e Edit  d Full diff"),
        (".tmplfile", "enter Fix  a Apply  m Merge  e Edit  d Full diff  t Rendered"),
        (".gone", "enter Fix  a Apply  e Edit  d Full diff"),
        ("hello.sh", "enter Fix  a Apply  d Full diff"),
    ],
)
async def test_the_key_bar_shows_what_the_row_admits(tmp_path, root, name, bar):
    app, _, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, name)
        assert key_bar(app) == bar + TAIL


async def test_one_selected_row_admitting_a_key_shows_it(tmp_path, root):
    app, _, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".gone", ".mine")
        await move_to(pilot, ".gone")
        assert "r Re-add" in key_bar(app)  # `.mine` admits it, `.gone` doesn't
        assert "m Merge" not in key_bar(app)  # `m` takes the current row


async def test_m_x_and_a_are_in_help_and_the_palette_not_the_bar(tmp_path, root):
    app, _, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        shown = [part.split(" ")[0] for part in key_bar(app).split("  ")]
        assert not {"M", "x", "A"} & set(shown)
        entries = await palette_entries(pilot)
        for entry in ("Merge through the selection", "Forget  x", "Apply all  A"):
            assert any(e.startswith(entry) for e in entries), entry
        await pilot.press("escape")
        await move_to(pilot, "hello.sh")  # a script: no merge, but forget and apply all
        entries = await palette_entries(pilot)
        assert not any(e.startswith("Merge") for e in entries)
        assert "Forget  x" in entries
        await pilot.press("escape", "question_mark")
        assert isinstance(app.screen, HelpScreen)
        help_lines = str(app.screen.query_one("#text").render()).splitlines()
        for key in ("M", "x", "A"):
            assert any(line.split()[:1] == [key] for line in help_lines), key


# --- `enter`: each direction's default fix (spec §6 Default fix) --------------------------


@pytest.mark.parametrize(
    ("name", "words"),
    [
        (".tool", ("apply", "--force", "--recursive=false")),
        (".mine", ("re-add", "--recursive=false")),
        (".gone", ("apply", "--force", "--recursive=false")),
        ("hello.sh", ("apply", "--force", "--recursive=false")),
    ],
)
async def test_enter_runs_the_default_fix_after_the_preview(tmp_path, root, name, words):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, name)
        await pilot.press("enter")
        await until_modal(pilot)
        assert writes(fake) == []
        await pilot.press("enter")
        await settled(pilot)
        assert writes(fake) == [(*words, str(root / "home" / name))]


async def test_enter_on_both_merges_suspended_without_a_modal(tmp_path, root):
    app, _, terminal = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".both file")
        await pilot.press("enter")
        await settled(pilot)
        assert terminal.calls == [
            ("chezmoi", "--no-tty", "merge", str(root / "home" / ".both file"))
        ]
        assert not [s for s in app.screen_stack if isinstance(s, PreviewModal)]


# --- The flow (spec §6 *Prepare*, *Running*, *Result*) -------------------------------------


async def test_a_on_a_selection_is_one_preview_and_one_call(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    home = root / "home"
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".tool", ".mine")
        release = fake.hold("dump-config")
        await pilot.press("a")
        await pilot.pause()
        assert log_line(app) == "preparing…"
        release.set()
        modal = await until_modal(pilot)
        assert [e.target.name for e in modal.plan.entries] == [".mine", ".tool"]
        await pilot.press("enter")
        await settled(pilot)
        assert writes(fake) == [
            ("apply", "--force", "--recursive=false", str(home / ".mine"), str(home / ".tool"))
        ]
        assert view(app).selected == set()
        assert statuses(fake) == 2  # the refresh after the write
        assert log_line(app) == "applied 2 targets (no commit)"
        assert logged(app) == ["applied 2 targets (no commit)"]


async def test_esc_cancels_the_preparation(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".mine")
        release = fake.hold("dump-config")
        await pilot.press("a")
        await until(pilot, lambda: any(c[:1] == ("dump-config",) for c in fake.chezmoi_calls))
        assert "a Apply" not in key_bar(app)  # no action key while preparing
        await pilot.press("escape")
        await pilot.pause()
        assert log_line(app) == "preview cancelled"
        assert logged(app) == ["preview cancelled"]
        assert app.writing is None
        release.set()
        await settled(pilot)
        assert not isinstance(app.screen, PreviewModal)
        assert writes(fake) == []
        assert view(app).selected == {root / "home" / ".mine"}
        assert statuses(fake) == 1  # no refresh: nothing ran


async def test_esc_cancels_the_preparation_before_a_read(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        read = fake.hold("symbolic-ref")
        await pilot.press("g")
        await until(pilot, lambda: any(c[3:4] == ("symbolic-ref",) for c in fake.log))
        assert app.reading
        preparation = fake.hold("dump-config")
        await pilot.press("a")  # a read already running goes on
        await until(pilot, lambda: app.writing is not None)
        await pilot.press("escape")
        await pilot.pause()
        assert app.writing is None
        assert app.reading  # the read wasn't cancelled
        assert logged(app) == ["preview cancelled"]
        read.set()
        preparation.set()
        await settled(pilot)
        assert log_line(app) == ""  # the read ended normally


async def test_g_and_action_keys_do_nothing_while_a_write_runs(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        release = fake.hold("rev-parse")  # HEAD before the command
        await pilot.press("enter")
        await until(pilot, lambda: app.writing == "running…")
        assert log_line(app) == "running…"
        await pilot.press("g", "a", "escape")
        await pilot.pause()
        assert statuses(fake) == 1
        assert not isinstance(app.screen, PreviewModal)
        assert app.writing == "running…"  # esc doesn't cancel a write
        assert log_line(app) == "running…"
        release.set()
        await settled(pilot)
        assert writes(fake) == [
            ("apply", "--force", "--recursive=false", str(root / "home" / ".tool"))
        ]
        assert statuses(fake) == 2


async def test_esc_in_the_preview_cancels_and_keeps_the_selection(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".mine")
        await pilot.press("r")
        await until_modal(pilot)
        assert log_line(app) == ""  # preparing has ended, nothing has run (DEC-36)
        await pilot.press("escape")
        await settled(pilot)
        assert log_line(app) == "preview cancelled"
        assert logged(app) == ["preview cancelled"]
        assert view(app).selected == {root / "home" / ".mine"}
        assert writes(fake) == []
        assert statuses(fake) == 1


async def test_the_frame_keys_do_nothing_in_the_preview(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        await pilot.press("R", "g", "l", "question_mark", "q", "1")
        await pilot.pause()
        assert isinstance(app.screen, PreviewModal)
        assert statuses(fake) == 1
        assert app.is_running


async def test_a_failed_preparation_is_shown_and_logged(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.fail("status", f"fatal: {TOKEN}\n".encode(), binary="git")
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await settled(pilot)
        # No target is known yet: the diagnostic is masked whole (DEC-30).
        assert log_line(app) == f"✗ git exited 1: {MASK}"
        assert [(e.text, e.diagnostic) for e in app.session_log] == [
            (f"✗ git exited 1: {MASK}", MASK)
        ]
        assert app.writing is None
        assert not isinstance(app.screen, PreviewModal)


# --- Blocked plans (spec §6 *The preview modal*) ----------------------------------------


@pytest.mark.parametrize("key", ["a", "e"])
async def test_a_blocked_plan_opens_the_modal_and_logs_at_once(tmp_path, root, key):
    app, fake, terminal = drift_app(root, tmp_path, auto_commit=True, stray=["dot_stray"])
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press(key)
        await until_modal(pilot)
        line = "✗ the source tree has uncommitted changes"
        assert log_line(app) == line
        assert [(e.text, e.diagnostic) for e in app.session_log] == [(line, "dot_stray")]
        await pilot.press("escape")
        await settled(pilot)
        assert len(app.session_log) == 1  # closing logs nothing more
        assert writes(fake) == []
        assert terminal.calls == []
        assert statuses(fake) == 1


async def test_a_failed_preview_opens_the_modal_and_logs_at_once(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    fake.fail("re-add", f"chezmoi: one\nchezmoi: {TOKEN}\n".encode(), binary="chezmoi")
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press("r")
        await until_modal(pilot)
        line = f"✗ preview failed: chezmoi: {MASK}"
        assert log_line(app) == line
        assert [(e.text, e.diagnostic) for e in app.session_log] == [
            (line, f"chezmoi: one\nchezmoi: {MASK}")
        ]


# --- Results (spec §6 *Result*) ----------------------------------------------------------


def commit_on(fake: FakeChezmoi, action: str, *, push: bool = False) -> None:
    """Move HEAD when `action` runs for real, as chezmoi's auto-commit does."""

    def reply(argv, env):
        words = subcommand(argv)
        if argv[0] == "chezmoi" and words[:1] == (action,) and "--dry-run" not in words:
            fake.commit("Update dot_mine")
            if push:
                assert fake.upstream is not None
                assert fake.head is not None
                fake.upstream.add(fake.head[0])
        return None

    fake.reply(reply)


@pytest.mark.parametrize(("push", "state"), [(False, "not pushed"), (True, "pushed")])
async def test_a_committed_re_add_names_the_commit(tmp_path, root, push, state):
    app, fake, _ = drift_app(root, tmp_path, auto_commit=True, upstream=set())
    commit_on(fake, "re-add", push=push)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press("r")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        assert fake.head is not None
        line = f're-added ~/.mine → {fake.head[0][:7]} "Update dot_mine" ({state})'
        assert log_line(app) == line  # still there after the refresh
        assert statuses(fake) == 2
        assert logged(app) == [line]


async def test_the_refresh_after_a_write_keeps_the_result_showing(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        release = fake.hold("symbolic-ref")
        await pilot.press("enter")
        await until(pilot, lambda: app.reading and app.writing is None)
        assert log_line(app) == "applied ~/.tool (no commit)"  # not `reading chezmoi…`
        release.set()
        await settled(pilot)
        assert log_line(app) == "applied ~/.tool (no commit)"


async def test_a_failed_apply(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)

    def reply(argv, env):
        words = subcommand(argv)
        if argv[0] == "chezmoi" and words[:1] == ("apply",) and "--dry-run" not in words:
            return Completed(argv, 1, b"", f"chezmoi: one\nchezmoi: {TOKEN}\n".encode())
        return None

    fake.reply(reply)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        line = f"✗ apply ~/.tool failed: chezmoi: {MASK}"
        assert log_line(app) == line  # one ✗, the line's own
        assert styles(app, "#log-line") == ["$error"]
        assert [(e.text, e.diagnostic) for e in app.session_log] == [
            (line, f"chezmoi: one\nchezmoi: {MASK}")
        ]
        assert statuses(fake) == 2  # a failure refreshes too


async def test_a_refresh_that_fails_after_a_write_replaces_the_result(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    broken = []

    def reply(argv, env):
        if broken and argv[0] == "chezmoi" and subcommand(argv)[:1] == ("status",):
            return Completed(argv, 1, b"", b"chezmoi: status broke\n")
        words = subcommand(argv)
        if argv[0] == "chezmoi" and words[:1] == ("apply",) and "--dry-run" not in words:
            broken.append(True)
        return None

    fake.reply(reply)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        assert log_line(app) == "✗ chezmoi exited 1: chezmoi: status broke"
        assert logged(app) == [
            "applied ~/.tool (no commit)",
            "✗ chezmoi exited 1: chezmoi: status broke",
        ]


# --- Interactive plans (spec §6 *Interactive plans*; DEC-4, DEC-29) ---------------------


@pytest.mark.parametrize(("key", "words"), [("e", ("edit", "--apply")), ("m", ("merge",))])
async def test_e_and_m_run_in_the_terminal_without_a_modal(tmp_path, root, key, words):
    app, fake, terminal = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press(key)
        await settled(pilot)
        assert terminal.calls == [("chezmoi", "--no-tty", *words, str(root / "home" / ".mine"))]
        assert not isinstance(app.screen, PreviewModal)
        verb = "edited" if key == "e" else "merged"
        assert log_line(app) == f"{verb} ~/.mine (no commit)"
        assert statuses(fake) == 2


@pytest.mark.parametrize(("key", "action"), [("e", "edit"), ("m", "merge")])
async def test_under_dry_run_e_and_m_dont_run(tmp_path, root, key, action):
    app, _, terminal = drift_app(root, tmp_path, dry_run=True)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press(key)
        await settled(pilot)
        assert terminal.calls == []
        assert log_line(app) == f"would {action} ~/.mine (dry run)"


async def test_m_on_a_selection_logs_the_rows_left_out(tmp_path, root):
    home = root / "home"
    (home / ".dir").mkdir(parents=True)
    fake_status = FakeChezmoi.drifted(root).status + f" M {home / '.dir'}\n"
    app, _, terminal = drift_app(root, tmp_path, status=fake_status)
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".dir", ".mine")
        await pilot.press("M")
        await settled(pilot)
        assert terminal.calls == [("chezmoi", "--no-tty", "merge", str(home / ".mine"))]
        assert logged(app) == [
            "left out of merge: ~/.dir (a directory)",
            "merged ~/.mine (no commit)",
        ]


async def test_a_mixed_enter_runs_the_modal_then_the_merge(tmp_path, root):
    app, fake, terminal = drift_app(root, tmp_path)
    home = root / "home"
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".both file", ".tool")
        await pilot.press("enter")
        await until_modal(pilot)
        assert terminal.calls == []
        await pilot.press("enter")
        await settled(pilot)
        assert writes(fake)[0] == ("apply", "--force", "--recursive=false", str(home / ".tool"))
        assert terminal.calls == [("chezmoi", "--no-tty", "merge", str(home / ".both file"))]
        assert log_line(app) == "applied ~/.tool (no commit); merged ~/.both file (no commit)"


# --- Apply all (spec §6 Apply all; DEC-16) -----------------------------------------------


async def test_apply_all_lists_every_row_whatever_the_filter_and_selection(tmp_path, root):
    app, _, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("slash", *"tool", "enter", "space")
        assert len(view(app).shown) == 1
        await pilot.press("A")
        modal = await until_modal(pilot)
        assert len(modal.plan.entries) == 6
        assert sum(e.selected for e in modal.plan.entries) == 3  # src→ and run


def only_local_changes(root: Path) -> str:
    """The recorded status with only its you→ and both rows."""
    lines = FakeChezmoi.drifted(root).status.splitlines(keepends=True)
    return "".join(line for line in lines if Path(line[3:].rstrip("\n")).name in KEPT)


KEPT = (".mine", ".both file")


async def test_apply_all_with_nothing_ticked_opens_the_preview(tmp_path, root):
    # The preview is where apply-all's rows get ticked (spec §6 The preview modal).
    app, fake, _ = drift_app(root, tmp_path, status=only_local_changes(root))
    async with app.run_test() as pilot:
        await settled(pilot)
        assert {r.target.name for r in view(app).shown} == set(KEPT)
        await pilot.press("A")
        modal = await until_modal(pilot)
        assert [(e.target.name, e.selected) for e in modal.plan.entries] == [
            (".both file", False),
            (".mine", False),
        ]
        assert footer(app) == "nothing ticked  space Tick  j/k Move  esc Cancel"
        assert writes(fake) == []
        assert "nothing is selected" not in log_line(app)
        await pilot.press("j", "space", "enter")
        await settled(pilot)
        home = root / "home"
        assert writes(fake) == [("apply", "--force", "--recursive=false", str(home / ".mine"))]
        assert logged(app) == ["applied ~/.mine (no commit)"]


async def test_esc_in_an_apply_all_with_nothing_ticked_cancels(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path, status=only_local_changes(root))
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".mine")
        await pilot.press("A")
        await until_modal(pilot)
        await pilot.press("escape")
        await settled(pilot)
        assert log_line(app) == "preview cancelled"
        assert logged(app) == ["preview cancelled"]
        assert view(app).selected == {root / "home" / ".mine"}
        assert writes(fake) == []
        assert statuses(fake) == 1  # no refresh


async def test_the_session_log_screen_shows_results(tmp_path, root):
    app, _, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        await pilot.press("l")
        assert isinstance(app.screen, SessionLogScreen)


# --- `d` (spec §6 `d`) ----------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
async def test_d_runs_chezmoi_diff_in_the_terminal(tmp_path, root, dry_run):
    app, fake, terminal = drift_app(root, tmp_path, dry_run=dry_run)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press("d")
        await settled(pilot)
        flags = ("--dry-run",) if dry_run else ()
        assert terminal.calls == [
            ("chezmoi", "--no-tty", *flags, "diff", str(root / "home" / ".mine"))
        ]
        # No pre-flight: git status ran once, for the header's read.
        assert sum(1 for c in fake.log if c[0] == "git" and c[3:4] == ("status",)) == 1


async def test_d_with_a_zero_exit_changes_nothing(tmp_path, root):
    app, fake, terminal = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("d")
        await settled(pilot)
        assert len(terminal.calls) == 1
        assert log_line(app) == ""
        assert app.session_log == []
        assert statuses(fake) == 1  # no refresh follows


async def test_d_with_a_non_zero_exit_says_so(tmp_path, root):
    app, fake, terminal = drift_app(root, tmp_path)
    terminal.code = 2
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("d")
        await settled(pilot)
        assert log_line(app) == "✗ chezmoi diff exited 2"
        assert logged(app) == ["✗ chezmoi diff exited 2"]
        assert statuses(fake) == 1


# --- Live: the installed chezmoi -----------------------------------------------------------


def live_app(scratch: Scratch) -> CheztuiApp:
    build_drift(scratch)
    scratch.commit_all("source changes")  # the drift stays; the tree is clean
    scratch.set_auto_commit(True)
    return CheztuiApp(Config(scratch.root / "cheztui.toml"), scratch.chezmoi())


@requires_chezmoi
async def test_live_re_add_commits(scratch: Scratch):
    app = live_app(scratch)
    before = scratch.git("rev-parse", "HEAD").stdout.strip()
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        await pilot.press("r")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        head = scratch.git("log", "-1", "--format=%H%x00%s").stdout.strip().split("\0")
        assert head[0] != before  # chezmoi's auto-commit
        assert (scratch.source / "dot_mine").read_text() == "mine\nedited in home\n"
        assert log_line(app) == f're-added ~/.mine → {head[0][:7]} "{head[1]}" (not pushed)'
        assert "~/.mine" not in [view(app).label(r) for r in view(app).rows or ()]


@requires_chezmoi
async def test_live_apply_writes_the_target(scratch: Scratch):
    app = live_app(scratch)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        await pilot.press("enter")
        await settled(pilot)
        assert (scratch.home / ".tool").read_text() == "#!/bin/sh\necho tool\necho more\n"
        assert log_line(app) == "applied ~/.tool (no commit)"


async def test_x_forgets_the_targets_after_the_preview(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await select(pilot, ".tool", ".mine")
        await pilot.press("x")
        modal = await until_modal(pilot)
        assert [c.action for c in modal.plan.commands] == ["forget"]
        await pilot.press("enter")
        await settled(pilot)
        home = root / "home"
        assert writes(fake) == [("forget", "--force", str(home / ".mine"), str(home / ".tool"))]
        assert log_line(app) == "forgot 2 targets (no commit)"


async def test_a_cancelled_refresh_after_a_write_replaces_the_result(tmp_path, root):
    app, fake, _ = drift_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        release = fake.hold("symbolic-ref")
        await pilot.press("enter")
        await until(pilot, lambda: app.reading and app.writing is None)
        assert log_line(app) == "applied ~/.tool (no commit)"
        await pilot.press("escape")
        await pilot.pause()
        assert log_line(app) == "refresh cancelled"
        assert logged(app) == ["applied ~/.tool (no commit)", "refresh cancelled"]
        release.set()
        await settled(pilot)
