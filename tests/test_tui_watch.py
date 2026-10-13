"""Watching in the TUI (spec §5 *Watching in the TUI*; DEC-37, DEC-38).

The app runs on `FakeChezmoi.drifted(root)` with the fake watchers conftest
installs: `due()` is a refresh falling due, `fail()` a watcher that stops.
Reads are counted by the fake's `status` calls. The real watcher runs in
`tests/test_tui_watch_live.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.pilot import Pilot

from cheztui.config import Config, RefreshConfig
from cheztui.runner import Completed
from cheztui.tui.app import CheztuiApp
from cheztui.tui.settings import SettingsScreen
from cheztui.tui.widgets.confirm import PreviewModal
from tests.fake_chezmoi import FakeChezmoi
from tests.fake_write import is_write, subcommand
from tests.test_tui_actions import (
    Terminal,
    log_line,
    logged,
    settled,
    statuses,
    until,
    until_modal,
    writes,
)
from tests.test_tui_drift import move_to
from tests.test_tui_screens import open_settings, row_text
from tests.watching import Watchers

WATCH_SET = ("managed", "--format=json", "--path-style=all", "--exclude=scripts")
RESULT = "applied 1 target (no commit)"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "scratch" / "home"))
    return tmp_path / "scratch"


def watch_app(
    root: Path, tmp_path: Path, refresh: RefreshConfig | None = None, dry_run: bool = False, **kw
) -> tuple[CheztuiApp, FakeChezmoi]:
    fake = FakeChezmoi.drifted(root, **kw)
    config = Config(tmp_path / "config.toml", refresh=refresh or RefreshConfig())
    app = CheztuiApp(config, fake.chezmoi(dry_run=dry_run), git_runner=fake.git_runner)
    app.terminal_runner = Terminal()
    return app, fake


def watch_sets(fake: FakeChezmoi) -> int:
    return sum(1 for c in fake.chezmoi_calls if c == WATCH_SET)


def branch_reads(fake: FakeChezmoi) -> int:
    return sum(1 for c in fake.log if c[0] == "git" and c[3:4] == ("symbolic-ref",))


def managed_targets(fake: FakeChezmoi) -> tuple[Path, ...]:
    entries = json.loads(fake.managed["-scripts"])
    return tuple(sorted(Path(e["absolute"]) for e in entries.values()))


async def applied(pilot: Pilot, name: str = ".tool") -> None:
    """Apply one row through its preview, up to the confirming `enter`."""
    await move_to(pilot, name)
    await pilot.press("a")
    await until_modal(pilot)


# --- The watch set (spec §5 *What is watched*, *The watch set*) ---------------------------


async def test_the_first_read_starts_the_watcher(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert len(watchers.made) == 1
        watcher = watchers.current
        assert watcher.source_dir == root / "source"  # the header's dump-config
        assert watcher.targets == managed_targets(fake)
        assert root / "home" / ".mine" in watcher.targets
        assert root / "home" / "hello.sh" not in watcher.targets  # a script
        assert watcher.debounce_ms == 300
        assert watcher.running
        assert watch_sets(fake) == 1


async def test_an_unchanged_set_keeps_the_watcher_a_changed_one_restarts_it(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("g")
        await settled(pilot)
        assert watch_sets(fake) == 2
        assert len(watchers.made) == 1
        assert watchers.current.running

        new = root / "home" / ".new"
        fake.managed["-scripts"] = json.dumps({".new": {"absolute": str(new)}})
        await pilot.press("g")
        await settled(pilot)
        assert len(watchers.made) == 2
        assert watchers.made[0].stopped
        assert watchers.current.targets == (new,)
        assert watchers.current.running

        fake.source = root / "elsewhere"  # the source dir moved; the targets didn't
        await pilot.press("g")
        await settled(pilot)
        assert len(watchers.made) == 3
        assert watchers.current.source_dir == root / "elsewhere"


async def test_a_failed_watch_set_read_logs_and_keeps_rows_and_set(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        rows = app.view.rows
        fake.reply(
            lambda argv, env: (
                Completed(argv, 1, b"", b"chezmoi: managed broke\n")
                if subcommand(argv) == WATCH_SET
                else None
            )
        )
        await pilot.press("g")
        await settled(pilot)
        assert log_line(app) == "✗ chezmoi exited 1: chezmoi: managed broke"
        assert logged(app) == ["✗ chezmoi exited 1: chezmoi: managed broke"]
        assert app.view.rows == rows
        assert len(watchers.made) == 1
        assert watchers.current.running
        assert watchers.current.targets == managed_targets(fake)


async def test_watch_off_at_start_reads_and_watches_nothing(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path, RefreshConfig(watch=False))
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("g")
        await settled(pilot)
        assert watchers.made == []
        assert watch_sets(fake) == 0
        assert not any("--exclude=scripts" in c for c in fake.chezmoi_calls)


async def test_dry_run_still_watches(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path, dry_run=True)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert watchers.current.running
        assert any(c[-1] == "--exclude=scripts" and "--dry-run" in c for c in fake.log)


# --- A watch refresh (spec §5 *A watch refresh*; DEC-37) -----------------------------------


async def test_a_due_refresh_is_one_quiet_read(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        app.set_log_line(RESULT)
        release = fake.hold("status")
        watchers.current.due()
        await until(pilot, lambda: statuses(fake) == 2)
        assert app.reading
        assert log_line(app) == RESULT  # never `reading chezmoi…`
        release.set()
        await settled(pilot)
        assert statuses(fake) == 2
        assert watch_sets(fake) == 2  # the whole read `g` runs
        assert log_line(app) == RESULT
        assert logged(app) == []


async def test_a_failing_watch_refresh_replaces_the_line(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        app.set_log_line(RESULT)
        fake.fail("status", b"chezmoi: status broke\n")
        watchers.current.due()
        await settled(pilot)
        assert log_line(app) == "✗ chezmoi exited 1: chezmoi: status broke"


async def test_esc_cancels_a_watch_refresh(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        app.set_log_line(RESULT)
        fake.hold("status")
        watchers.current.due()
        await until(pilot, lambda: app.reading)
        await pilot.press("escape")
        await pilot.pause()
        assert log_line(app) == "refresh cancelled"
        assert logged(app) == ["refresh cancelled"]
        assert not app.reading


async def test_due_mid_read_waits_and_runs_once_after(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("status")
        await pilot.press("g")
        await until(pilot, lambda: statuses(fake) == 2)
        watchers.current.due()
        watchers.current.due()
        await pilot.pause()
        assert statuses(fake) == 2  # waiting, not cancelling `g`'s read
        release.set()
        await settled(pilot)
        assert statuses(fake) == 3
        assert "refresh cancelled" not in logged(app)
        assert log_line(app) == ""  # `g`'s read emptied it; the quiet one kept that


async def test_esc_drops_a_refresh_waiting_on_the_read(tmp_path, root, watchers: Watchers):
    # DEC-38: the user stopped reading; the next change or `g` reads again.
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.hold("status")
        await pilot.press("g")
        await until(pilot, lambda: statuses(fake) == 2)
        watchers.current.due()
        await pilot.press("escape")
        await settled(pilot)
        assert statuses(fake) == 2
        assert log_line(app) == "refresh cancelled"


# --- cheztui's own writes (spec §5 *cheztui's own writes*; DEC-37) -------------------------


async def test_due_while_a_preview_is_open_runs_after_esc(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await applied(pilot)
        watchers.current.due()
        await pilot.pause()
        assert statuses(fake) == 1
        await pilot.press("escape")
        await settled(pilot)
        assert statuses(fake) == 2
        assert log_line(app) == "preview cancelled"  # quiet: the line stays
        assert watchers.current.calls == []  # nothing executed: no pause


async def test_due_while_preparing_runs_after_the_preparation_is_cancelled(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.hold("dump-config")
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until(pilot, lambda: app.writing is not None)
        watchers.current.due()
        await pilot.press("escape")
        fake.holds.pop("dump-config").set()
        await settled(pilot)
        assert statuses(fake) == 2
        assert log_line(app) == "preview cancelled"


async def test_due_while_preparing_runs_after_the_preparation_fails(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("dump-config")
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until(pilot, lambda: app.writing is not None)
        watchers.current.due()
        fake.fail("dump-config", b"chezmoi: config broke\n")
        release.set()
        await settled(pilot)
        assert writes(fake) == []
        assert statuses(fake) == 2


async def test_due_while_a_blocked_preview_is_open_runs_after(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path, auto_commit=True, stray=["dot_stray"])
    async with app.run_test() as pilot:
        await settled(pilot)
        await applied(pilot)
        assert isinstance(app.screen, PreviewModal)
        assert app.screen.plan.blocked
        watchers.current.due()
        await pilot.pause()
        assert statuses(fake) == 1
        await pilot.press("escape")
        await settled(pilot)
        assert statuses(fake) == 2


async def test_due_while_preparing_a_plan_that_executes_is_dropped(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("dump-config")
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until(pilot, lambda: app.writing is not None)
        watchers.current.due()
        release.set()
        await until_modal(pilot)
        watchers.current.due()  # and while its preview is open
        await pilot.press("enter")
        await settled(pilot)
        assert len(writes(fake)) == 1
        assert statuses(fake) == 2  # only the refresh after the write
        assert log_line(app) == "applied ~/.tool (no commit)"


async def test_execution_pauses_watching_until_its_refresh_ends(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    at_command: list[list[str]] = []

    def spy(argv, env):
        if is_write(argv) and "--dry-run" not in argv:
            at_command.append(list(watchers.current.calls))
        return None

    async with app.run_test() as pilot:
        await settled(pilot)
        fake.reply(spy)
        await applied(pilot)
        release = fake.hold("symbolic-ref")  # the header read: `status` is the pre-flight's too
        await pilot.press("enter")
        await until(pilot, lambda: branch_reads(fake) == 2)
        assert at_command == [["pause"]]  # paused before the command ran
        assert watchers.current.calls == ["pause"]  # the refresh hasn't ended
        watchers.current.due()  # dropped: the write's refresh covers it
        release.set()
        await settled(pilot)
        assert watchers.current.calls == ["pause", "resume"]
        assert statuses(fake) == 2
        watchers.current.due()  # watching again
        await settled(pilot)
        assert statuses(fake) == 3


async def test_esc_on_the_write_refresh_resumes_watching(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await applied(pilot)
        fake.hold("symbolic-ref")
        await pilot.press("enter")
        await until(pilot, lambda: branch_reads(fake) == 2)
        assert watchers.current.calls == ["pause"]
        await pilot.press("escape")
        await pilot.pause()
        assert log_line(app) == "refresh cancelled"
        assert watchers.current.calls == ["pause", "resume"]


async def test_g_replacing_the_write_refresh_resumes_when_it_ends(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await applied(pilot)
        release = fake.hold("symbolic-ref")  # the header read: `status` is the pre-flight's too
        await pilot.press("enter")
        await until(pilot, lambda: branch_reads(fake) == 2)
        await pilot.press("g")
        await until(pilot, lambda: branch_reads(fake) == 3)
        assert watchers.current.calls == ["pause"]
        release.set()
        await settled(pilot)
        assert watchers.current.calls == ["pause", "resume"]


async def test_a_watcher_restarted_during_a_pause_starts_paused(tmp_path, root, watchers: Watchers):
    app, fake = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await applied(pilot)
        new = root / "home" / ".new"
        fake.managed["-scripts"] = json.dumps({".new": {"absolute": str(new)}})
        await pilot.press("enter")
        await settled(pilot)
        assert len(watchers.made) == 2
        assert watchers.current.calls == ["pause", "resume"]


# --- Settings (spec §5 *Settings*, §9) ------------------------------------------------------


async def test_toggling_watch_in_settings_stops_and_restarts(tmp_path, root, watchers: Watchers):
    app, _ = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await open_settings(pilot, "refresh.watch")
        first = watchers.current
        await pilot.press("enter")
        await pilot.pause()
        assert row_text(app, "refresh.watch") == "refresh.watch  false"
        assert first.stopped
        assert app.watcher is None
        await pilot.press("enter")
        await pilot.pause()
        assert len(watchers.made) == 2
        assert watchers.current.running
        assert watchers.current.targets == first.targets  # the last watch set


async def test_watch_turned_on_with_no_set_starts_after_the_next_read(
    tmp_path, root, watchers: Watchers
):
    app, fake = watch_app(root, tmp_path, RefreshConfig(watch=False))
    async with app.run_test() as pilot:
        await open_settings(pilot, "refresh.watch")
        await pilot.press("enter")
        await pilot.pause()
        assert watchers.made == []
        await pilot.press("escape")
        await pilot.press("g")
        await settled(pilot)
        assert watchers.current.running
        assert watchers.current.targets == managed_targets(fake)


async def test_a_new_debounce_restarts_the_watcher(tmp_path, root, watchers: Watchers):
    app, _ = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await open_settings(pilot, "refresh.debounce_ms")
        await pilot.press("enter")
        await pilot.press(*"50", "enter")  # the value is selected: typing replaces it
        await pilot.pause()
        assert row_text(app, "refresh.debounce_ms") == "refresh.debounce_ms  50"
        assert len(watchers.made) == 2
        assert watchers.made[0].stopped
        assert watchers.current.debounce_ms == 50


async def test_a_change_made_through_settings_e_takes_effect(tmp_path, root, watchers: Watchers):
    app, _ = watch_app(root, tmp_path)

    def editor(path: Path) -> None:
        path.write_text("[refresh]\nwatch = false\n")

    app.launch_editor = editor
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        await pilot.press("e")
        await pilot.pause()
        assert watchers.current.stopped
        assert app.watcher is None
        app.launch_editor = lambda path: path.write_text("[refresh]\ndebounce_ms = 70\n")
        await pilot.press("e")
        await pilot.pause()
        assert len(watchers.made) == 2
        assert watchers.current.running
        assert watchers.current.debounce_ms == 70
        assert isinstance(app.screen, SettingsScreen)


# --- Failure and quitting (spec §5 *Failure*) -----------------------------------------------


async def test_a_failing_watcher_says_so_and_g_restarts_it(tmp_path, root, watchers: Watchers):
    app, _ = watch_app(root, tmp_path)
    token = "ghp_" + "a" * 36
    async with app.run_test() as pilot:
        await settled(pilot)
        app.set_log_line(RESULT)
        watchers.current.fail(OSError(28, f"watch limit reached {token}"))
        await until(pilot, lambda: app.watcher is None)
        line = log_line(app)
        assert line.startswith("✗ watching stopped: [Errno 28] watch limit reached")
        assert token not in line  # masked as a read diagnostic
        assert logged(app) == [line]
        assert app.session_log[0].diagnostic is not None
        assert token not in app.session_log[0].diagnostic
        await pilot.press("g")
        await settled(pilot)
        assert len(watchers.made) == 2  # the same set, started again
        assert watchers.current.targets == watchers.made[0].targets
        assert watchers.current.running


@pytest.mark.parametrize("key", ["q", None])
async def test_quitting_with_the_watcher_running_exits(tmp_path, root, watchers: Watchers, key):
    app, _ = watch_app(root, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert watchers.current.running
        if key:
            await pilot.press(key)
            await pilot.pause()
    assert not watchers.current.running
