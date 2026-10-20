"""The app frame: header, tab, panes, log line and reads (spec §4, §5; DEC-20, DEC-33).

Every test but the one marked `requires_chezmoi` runs on `FakeChezmoi`; that
one runs the real chezmoi in the scratch setup inside Textual's loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Static
from textual.worker import WorkerCancelled, WorkerState

from cheztui.config import Config, SecretsConfig, UiConfig
from cheztui.tui.app import CheztuiApp
from cheztui.tui.views.drift import DriftView
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_chezmoi import FakeChezmoi
from tests.test_chezmoi_reads import ROOT, build_drift

TOKEN = "ghp_" + "a" * 36


def make_app(
    fake: FakeChezmoi, tmp_path: Path, *, ui: UiConfig | None = None, **flags
) -> CheztuiApp:
    config = Config(tmp_path / "config.toml", ui=ui or UiConfig())
    return CheztuiApp(config, fake.chezmoi(**flags), git_runner=fake.git_runner)


async def settled(pilot: Pilot) -> None:
    """Wait for the workers that end: the watcher's runs until it is stopped.

    One cancelled while waited for (a body read the next one supersedes)
    raises out of `wait_for_complete` before the rest are done, so wait again.
    """
    for _ in range(200):
        workers = [w for w in pilot.app.workers if w.group != "watch" and not w.is_cancelled]
        try:
            if workers:  # an empty list waits for every worker
                await pilot.app.workers.wait_for_complete(workers)
        except WorkerCancelled:
            continue
        await pilot.pause()
        return
    raise AssertionError("workers never settled")


def text(app: CheztuiApp, selector: str) -> str:
    return str(app.query_one(selector, Static).render())


def styles(app: CheztuiApp, selector: str) -> list[str]:
    content = app.query_one(selector, Static).render()
    assert isinstance(content, Content)
    return [str(span.style) for span in content.spans]


def header(app: CheztuiApp) -> tuple[str, str]:
    return text(app, "#header-left"), text(app, "#header-right")


def listed(app: CheztuiApp) -> list[str]:
    """The list pane as shown: its rows, or its one line of message."""
    view = app.query_one(DriftView)
    if view.rows:
        return [view.label(r) for r in view.rows]
    message = text(app, "#list-message")
    return [message] if message else []


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    # The fixtures' targets are under /scratch/home; their source isn't.
    monkeypatch.setenv("HOME", str(ROOT / "home"))
    return ROOT / "home"


# --- Header -------------------------------------------------------------------------


async def test_settled_waits_past_a_cancelled_worker(tmp_path):
    # #45: a worker cancelled while `settled` waits on it doesn't end the wait.
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = asyncio.Event()

        async def work() -> None:
            await release.wait()

        first = app.run_worker(work(), group="probe", exclusive=True)
        waiting = asyncio.create_task(settled(pilot))
        await asyncio.sleep(0)  # `settled` is now waiting on `first`
        second = app.run_worker(work(), group="probe", exclusive=True)
        await pilot.pause()
        assert first.is_cancelled
        assert not waiting.done()
        release.set()
        await waiting
        assert first.is_finished
        assert second.is_finished
        assert second.state == WorkerState.SUCCESS


async def test_header_on_drift_with_an_upstream(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(ROOT))  # the source is under $HOME here
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2), stray=["a", "b"])
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app) == ("cheztui • ~/source", "✗ verify   master ↑1 ↓2   2 uncommitted")


async def test_header_in_sync_without_upstream(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app) == ("cheztui • /s", "✓ verify   master   clean")


async def test_header_detached(tmp_path):
    app = make_app(FakeChezmoi.in_sync(branch=None), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app)[1] == "✓ verify   detached   clean"


@pytest.mark.parametrize("dry_run", [False, True])
async def test_dry_run_marker(tmp_path, dry_run):
    fake = FakeChezmoi.in_sync()
    app = make_app(fake, tmp_path, dry_run=dry_run)
    async with app.run_test() as pilot:
        await settled(pilot)
        right = header(app)[1]
        assert right.endswith("   DRY RUN") is dry_run
        assert ("bold $warning" in styles(app, "#header-right")) is dry_run
        assert ("DRY RUN" in right) is dry_run
    # -n reaches every chezmoi call but the version check, which has no global flags.
    assert (
        all("--dry-run" in c for c in fake.log if c[0] == "chezmoi" and c[1:] != ("--version",))
        is dry_run
    )


async def test_before_the_first_read_finishes(tmp_path):
    fake = FakeChezmoi.drifted()
    release = fake.hold("--version")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert header(app) == ("cheztui", "")
        assert text(app, "#log-line") == "reading chezmoi…"
        assert text(app, "#tabs").strip() == "1 Drift"
        assert listed(app) == []
        release.set()
        await settled(pilot)
        assert text(app, "#log-line") == ""
        assert len(listed(app)) == 6


# --- Tabs (DEC-20) ---------------------------------------------------------------


async def test_the_one_tab_counts_drift_rows(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#tabs").strip() == "1 Drift 6"


async def test_the_tab_has_no_count_in_sync(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#tabs").strip() == "1 Drift"
        assert listed(app) == ["✓ in sync"]


async def test_missing_views_numbers_do_nothing(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        before = (len(fake.log), header(app), text(app, "#tabs"), listed(app))
        for key in "2345678901":
            await pilot.press(key)
        await settled(pilot)
        assert (len(fake.log), header(app), text(app, "#tabs"), listed(app)) == before


# --- Failures ---------------------------------------------------------------------


async def test_a_failed_status_clears_the_list(tmp_path):
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2))
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.fail("status", b"chezmoi: some context\nchezmoi: status broke\n", binary="chezmoi")
        await pilot.press("g")
        await settled(pilot)
        message = "✗ chezmoi exited 1: chezmoi: status broke"
        assert text(app, "#log-line") == message
        assert styles(app, "#log-line") == ["$error"]
        assert listed(app) == [message]
        assert text(app, "#tabs").strip() == "1 Drift"
        # The header's own read succeeded; the verify badge goes with the Drift data.
        assert header(app) == ("cheztui • /scratch/source", "master ↑1 ↓2   clean")


@pytest.mark.parametrize("command", ["symbolic-ref", "status"])
async def test_a_failed_git_read_blanks_the_header(tmp_path, command):
    fake = FakeChezmoi.drifted()
    fake.fail(command, b"fatal: not a git repository\n", 128, binary="git")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app) == ("cheztui", "✗ verify")
        assert len(listed(app)) == 6
        assert text(app, "#log-line") == "✗ git exited 128: fatal: not a git repository"


async def test_a_failed_dump_config_blanks_the_header(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.fail("dump-config", b"chezmoi: bad config\n")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app) == ("cheztui", "✗ verify")
        assert text(app, "#log-line") == "✗ chezmoi exited 1: chezmoi: bad config"


async def test_a_header_failure_clears_earlier_header_data(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app)[0] == "cheztui • /scratch/source"
        fake.fail("rev-parse", b"fatal: bad\n", 128)
        await pilot.press("g")
        await settled(pilot)
        assert header(app) == ("cheztui", "✗ verify")


async def test_chezmoi_too_old_leaves_everything_blank(tmp_path):
    fake = FakeChezmoi.drifted(version="chezmoi version v2.59.0, commit x")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert header(app) == ("cheztui", "")
        assert listed(app) == []
        assert text(app, "#tabs").strip() == "1 Drift"
        assert text(app, "#log-line") == (
            "✗ chezmoi 2.59.0 is too old: cheztui needs 2.60.0 or later"
        )
        assert fake.chezmoi_calls == [()]  # the version check alone


async def test_chezmoi_missing(tmp_path):
    app = make_app(FakeChezmoi.drifted(version=None), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#log-line") == "✗ chezmoi not found"
        assert listed(app) == []


async def test_a_failure_is_masked(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.fail("status", f"chezmoi: auth failed for {TOKEN}\n".encode(), binary="chezmoi")
    fake.fail("dump-config", f"chezmoi: password = {TOKEN}\n".encode())
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#log-line") == "✗ chezmoi exited 1: chezmoi: auth failed for ********"
        assert listed(app) == ["✗ chezmoi exited 1: chezmoi: auth failed for ********"]
        logged = [(e.text, e.diagnostic) for e in app.session_log]
        assert logged == [
            (
                "✗ chezmoi exited 1: chezmoi: auth failed for ********",
                "chezmoi: auth failed for ********",
            ),
            ("✗ chezmoi exited 1: chezmoi: password = ********", "chezmoi: password = ********"),
        ]
        assert TOKEN not in repr(app.session_log)


async def test_masking_ignores_never_mask(tmp_path):
    # Read diagnostics can't be assigned to a target, so `never_mask` doesn't apply (DEC-30).
    fake = FakeChezmoi.drifted()
    fake.fail("status", f"chezmoi: {TOKEN}\n".encode(), binary="chezmoi")
    config = Config(tmp_path / "c.toml", secrets=SecretsConfig(never_mask=("/**",)))
    app = CheztuiApp(config, fake.chezmoi(), git_runner=fake.git_runner)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert TOKEN not in text(app, "#log-line")


# --- Refresh and cancellation ----------------------------------------------------


def calls(fake: FakeChezmoi, command: str) -> int:
    return sum(1 for c in fake.chezmoi_calls if c[:1] == (command,))


async def test_g_rereads_both(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        # dump-config: the header's read, and the detail's destination (spec §6).
        assert (calls(fake, "status"), calls(fake, "dump-config")) == (1, 2)
        await pilot.press("g")
        await settled(pilot)
        assert (calls(fake, "status"), calls(fake, "dump-config")) == (2, 4)
        # The version check passed once and isn't repeated (DEC-21).
        assert fake.chezmoi_calls.count(()) == 1


async def test_esc_cancels_a_read_and_keeps_the_data(tmp_path):
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2))
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        before = (header(app), listed(app), text(app, "#tabs"))
        release = fake.hold("status")
        fake.stray = ["new"]
        await pilot.press("g")
        await pilot.pause()
        assert text(app, "#log-line") == "reading chezmoi…"
        await pilot.press("escape")
        await settled(pilot)
        assert text(app, "#log-line") == "refresh cancelled"
        assert listed(app) == before[1]
        assert text(app, "#tabs") == before[2]
        assert [e.text for e in app.session_log] == ["refresh cancelled"]
        release.set()
        await settled(pilot)
        assert text(app, "#log-line") == "refresh cancelled"


async def test_a_failure_shown_before_esc_is_logged(tmp_path):
    # `status` fails while the header's read is held; `esc` cancels the read
    # after the list shows the error, which is already in the log (#31).
    fake = FakeChezmoi.drifted()
    fake.fail("status", b"chezmoi: status broke\n", binary="chezmoi")
    release = fake.hold("dump-config")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        for _ in range(100):
            if text(app, "#list-message"):
                break
            await pilot.pause()
        assert listed(app) == ["✗ chezmoi exited 1: chezmoi: status broke"]
        await pilot.press("escape")
        await settled(pilot)
        logged = [(e.text, e.diagnostic) for e in app.session_log]
        assert logged == [
            ("✗ chezmoi exited 1: chezmoi: status broke", "chezmoi: status broke"),
            ("refresh cancelled", None),
        ]
        release.set()
        await settled(pilot)
        assert [(e.text, e.diagnostic) for e in app.session_log] == logged
        assert text(app, "#log-line") == "refresh cancelled"


async def test_esc_without_a_read_does_nothing(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("escape")
        await settled(pilot)
        assert text(app, "#log-line") == ""
        assert app.session_log == []


async def test_g_during_a_read_cancels_it(tmp_path):
    fake = FakeChezmoi.drifted()
    release = fake.hold("status")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = [w for w in app.workers if w.group == "read"]
        assert len(first) == 1
        await pilot.press("g")
        await pilot.pause()
        assert first[0].is_cancelled
        assert text(app, "#log-line") == "reading chezmoi…"
        release.set()
        await settled(pilot)
        assert calls(fake, "status") == 2
        assert text(app, "#log-line") == ""
        assert len(listed(app)) == 6
        assert app.session_log == []  # a superseded read isn't a cancellation


async def test_g_after_a_failed_version_check_retries_it(tmp_path):
    fake = FakeChezmoi.drifted(version=None)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#log-line") == "✗ chezmoi not found"
        fake.version = "chezmoi version v2.71.0, commit x"
        await pilot.press("g")
        await settled(pilot)
        assert fake.chezmoi_calls.count(()) == 2
        assert text(app, "#log-line") == ""
        assert len(listed(app)) == 6


async def test_a_successful_read_clears_an_earlier_error(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.fail("status", b"chezmoi: once\n", binary="chezmoi")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#log-line").startswith("✗ ")
        fake.replies.clear()
        await pilot.press("g")
        await settled(pilot)
        assert text(app, "#log-line") == ""
        assert text(app, "#tabs").strip() == "1 Drift 6"


async def test_q_quits(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("q")
    assert app.return_code == 0


# --- Panes and layout ----------------------------------------------------------------


def panes(app: CheztuiApp) -> tuple[str, bool]:
    list_pane, detail = app.query_one("#list-pane"), app.query_one("#detail")
    side_by_side = list_pane.region.y == detail.region.y
    return ("side" if side_by_side else "stacked"), app.narrow


async def test_layout_switches_at_the_breakpoint(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path, ui=UiConfig(narrow_breakpoint=100))
    async with app.run_test(size=(99, 32)) as pilot:
        await settled(pilot)
        assert panes(app) == ("stacked", True)
        list_pane, detail = app.query_one("#list-pane"), app.query_one("#detail")
        assert abs(list_pane.region.height - detail.region.height) <= 1
        await pilot.resize_terminal(100, 32)
        await pilot.pause()
        assert panes(app) == ("side", False)
        assert list_pane.region.width * 3 == pytest.approx(detail.outer_size.width * 2, abs=3)
        await pilot.resize_terminal(99, 32)
        await pilot.pause()
        assert panes(app) == ("stacked", True)


async def test_theme_comes_from_the_config(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path, ui=UiConfig(theme="nord"))
    async with app.run_test() as pilot:
        await settled(pilot)
        assert app.theme == "nord"


# --- Live --------------------------------------------------------------------------


@requires_chezmoi
async def test_live_frame(scratch: Scratch, monkeypatch: pytest.MonkeyPatch):
    # The ops reads were written for asyncio.run in the CLI: this holds them to Textual's loop.
    monkeypatch.setenv("HOME", str(scratch.home))
    build_drift(scratch)
    scratch.commit_all("source changes")  # the drift stays; the tree is clean
    config = Config(scratch.root / "cheztui.toml")
    app = CheztuiApp(config, scratch.chezmoi())
    async with app.run_test() as pilot:
        await settled(pilot)
        left, right = header(app)
        assert left == f"cheztui • {scratch.source}"
        assert right.endswith("   master   clean")
        assert "~/.mine" in listed(app)
        assert text(app, "#log-line") == ""


# --- Snapshots ------------------------------------------------------------------------


def test_snapshot_drifted(snap_compare, tmp_path):
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2), stray=["a", "b"])
    assert snap_compare(make_app(fake, tmp_path), terminal_size=(120, 32), run_before=settled)


def test_snapshot_in_sync(snap_compare, tmp_path):
    fake = FakeChezmoi.in_sync()
    assert snap_compare(make_app(fake, tmp_path), terminal_size=(120, 32), run_before=settled)


def test_snapshot_narrow(snap_compare, tmp_path):
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2), stray=["a", "b"])
    assert snap_compare(make_app(fake, tmp_path), terminal_size=(99, 32), run_before=settled)


async def test_reads_run_concurrently(tmp_path):
    # The header's read doesn't wait for the Drift read (spec §5 Reads in the TUI).
    fake = FakeChezmoi.drifted()
    release = fake.hold("managed")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause()
            if header(app)[0] != "cheztui":
                break
        assert header(app) == ("cheztui • /scratch/source", "master   clean")
        release.set()
        await settled(pilot)
        assert header(app)[1] == "✗ verify   master   clean"
