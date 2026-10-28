"""Watching end to end (spec §5 *Watching in the TUI*; DEC-37).

The real `watch.Watcher` on real files: `FakeChezmoi.drifted(root)` puts the
home's files under `tmp_path`, and the source dir, with a `.git/`, is made
beside it. Reads are counted by the fake's `status` calls. Negatives use the
sentinel pattern in `tests/watching.py`, never a bare sleep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cheztui.config import Config, RefreshConfig
from cheztui.tui.app import CheztuiApp
from cheztui.watch import Watcher
from tests.fake_chezmoi import FakeChezmoi
from tests.fake_write import is_write
from tests.test_tui_actions import Terminal, settled, statuses, until_modal
from tests.test_tui_drift import move_to
from tests.watching import Events, after_sentinel, eventually, quiet, ready, record_events

DEBOUNCE = 100


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "scratch" / "home"))
    return tmp_path / "scratch"


def live_app(root: Path, tmp_path: Path, *, watch: bool = True) -> tuple[CheztuiApp, FakeChezmoi]:
    fake = FakeChezmoi.drifted(root)
    (fake.source / ".git").mkdir(parents=True)
    config = Config(tmp_path / "config.toml", refresh=RefreshConfig(watch, DEBOUNCE))
    app = CheztuiApp(config, fake.chezmoi(), git_runner=fake.git_runner)
    app.watcher_factory = Watcher  # the real one, not conftest's fake
    app.terminal_runner = Terminal()
    return app, fake


class Reads:
    """The fake's `status` calls since the watcher became live; `events` records
    what watchfiles reports, with this count as each batch arrived."""

    def __init__(self, fake: FakeChezmoi, events: Events) -> None:
        self.fake = fake
        self.events = events
        self.base = 0
        events.count = self

    def __call__(self) -> int:
        return statuses(self.fake) - self.base

    async def ready(self, home: Path) -> None:
        await ready(home / ".tmplfile", lambda: statuses(self.fake), self.events, DEBOUNCE)
        self.base = statuses(self.fake)

    async def sentinel(self, home: Path) -> None:
        await after_sentinel(home / ".mine", self.events, DEBOUNCE)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Events:
    return record_events(monkeypatch)


async def test_a_managed_target_edit_is_one_refresh(tmp_path, root, events):
    app, fake = live_app(root, tmp_path)
    home = root / "home"
    reads = Reads(fake, events)
    async with app.run_test() as pilot:
        await settled(pilot)
        await reads.ready(home)
        (home / ".tool").write_text("edited elsewhere\n")
        await eventually(lambda: reads() >= 1)
        await reads.sentinel(home)
        assert reads() == 2, events.describe()


async def test_a_source_edit_is_a_refresh(tmp_path, root, events):
    app, fake = live_app(root, tmp_path)
    reads = Reads(fake, events)
    async with app.run_test() as pilot:
        await settled(pilot)
        await reads.ready(root / "home")
        (fake.source / "dot_new").write_text("new\n")
        await eventually(lambda: reads() >= 1)


async def test_a_sibling_and_git_change_refresh_nothing(tmp_path, root, events):
    app, fake = live_app(root, tmp_path)
    home = root / "home"
    reads = Reads(fake, events)
    async with app.run_test() as pilot:
        await settled(pilot)
        await reads.ready(home)
        (home / ".unmanaged").write_text("not managed\n")
        (fake.source / ".git" / "index").write_text("git's own\n")
        await reads.sentinel(home)
        assert reads() == 1, events.describe()


async def test_a_write_that_touches_a_target_is_one_refresh(tmp_path, root, events):
    app, fake = live_app(root, tmp_path)
    home = root / "home"
    reads = Reads(fake, events)

    def apply_writes_the_target(argv, env):
        # chezmoi's apply writes the target: its events must not refresh again.
        if is_write(argv) and "--dry-run" not in argv:
            (home / ".tool").write_text("applied\n")
        return None

    fake.reply(apply_writes_the_target)
    async with app.run_test() as pilot:
        await settled(pilot)
        await reads.ready(home)
        await move_to(pilot, ".tool")
        await pilot.press("a")
        await until_modal(pilot)
        # Hold the refresh after the write until the write's events have arrived.
        release = fake.hold("symbolic-ref")
        mark = len(events.arrived)
        await pilot.press("enter")
        await eventually(lambda: events.saw(".tool", mark) is not None)
        await quiet(lambda: len(events.arrived), DEBOUNCE)
        assert reads() == 1, events.describe()  # the refresh after the write, still running
        release.set()
        await settled(pilot)
        await reads.sentinel(home)
        assert reads() == 2, events.describe()


async def test_watch_off_refreshes_on_g_only(tmp_path, root, events):
    app, fake = live_app(root, tmp_path, watch=False)
    home = root / "home"
    async with app.run_test() as pilot:
        await settled(pilot)
        assert app.watcher is None
        (home / ".tool").write_text("edited elsewhere\n")
        await settled(pilot)
        assert statuses(fake) == 1
        assert events.arrived == []  # nothing is watched at all
        await pilot.press("g")
        await settled(pilot)
        assert statuses(fake) == 2
