"""Watching (spec §5 *Watching in the TUI*, §10; DEC-37).

`counts` is pure. The `Watcher` tests run real watchfiles on `tmp_path`
dirs, with no chezmoi: negatives use the sentinel pattern in
`tests/watching.py`, never a bare sleep. `watch_targets` runs against the
real chezmoi in the scratch setup.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cheztui import ops, watch
from cheztui.watch import Watcher, counts
from tests.conftest import Scratch, requires_chezmoi
from tests.watching import Events, after_sentinel, eventually, quiet, ready, record_events

DEBOUNCE = 100


# --- Which events count -------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """`source/` and `home/`, with a symlinked dir and a managed symlink in the home."""
    (tmp_path / "source" / "sub").mkdir(parents=True)
    home = tmp_path / "home"
    (home / "real").mkdir(parents=True)
    (home / "linked").symlink_to(home / "real")
    (home / ".mine").write_text("mine\n")
    (home / ".link").symlink_to(".mine")
    return tmp_path


def test_counts_source_files(tree: Path):
    source = tree / "source"
    assert counts(source / "dot_zshrc", source, [])
    assert counts(source / "sub" / "dot_x", source, [])
    assert counts(source, source, [])


def test_counts_skips_git_below_the_source(tree: Path):
    source = tree / "source"
    assert not counts(source / ".git" / "index", source, [])
    assert not counts(source / "sub" / ".git" / "HEAD", source, [])
    assert counts(source / ".gitignore", source, [])
    assert counts(source / "dot_git" / "x", source, [])


def test_counts_skips_git_above_the_source_only_below(tree: Path):
    # A `.git` component above the source dir isn't below it.
    source = tree / ".git" / "source"
    source.mkdir(parents=True)
    assert counts(source / "dot_x", source, [])


def test_counts_a_managed_target_not_its_sibling(tree: Path):
    home = tree / "home"
    targets = [home / ".zshrc"]
    assert counts(home / ".zshrc", tree / "source", targets)
    assert not counts(home / ".zshrc.bak", tree / "source", targets)
    assert not counts(home / "sub" / ".zshrc", tree / "source", targets)


def test_counts_a_target_through_a_symlinked_parent(tree: Path):
    # Watched as the link, reported resolved (macOS reports realpaths).
    home = tree / "home"
    target = home / "linked" / "config"
    assert counts(home / "real" / "config", tree / "source", [target])
    assert counts(target, tree / "source", [target])


def test_counts_a_managed_symlink_by_its_own_name(tree: Path):
    # Resolving the target itself would make it `.mine`, which isn't managed.
    home = tree / "home"
    assert counts(home / ".link", tree / "source", [home / ".link"])
    assert not counts(home / ".mine", tree / "source", [home / ".link"])


def test_counts_a_symlinked_source_dir(tree: Path):
    real = tree / "source"
    link = tree / "source-link"
    link.symlink_to(real)
    assert counts(real / "dot_x", link, [])
    assert counts(link / "dot_x", link, [])
    assert not counts(link / ".git" / "index", link, [])


def test_watch_imports_neither_textual_nor_chezmoi():
    # spec §10: watch.py knows nothing of Textual or chezmoi.
    tree = ast.parse(Path(watch.__file__).read_text())
    imported = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        )
    }
    assert not any(n.split(".")[0] == "textual" for n in imported)
    assert not any(n.startswith("cheztui") for n in imported)


# --- The watcher ----------------------------------------------------------------------


def test_a_failing_count_shows_its_events():
    events = Events()
    assert events.describe() == "(no events)"
    events.arrived.extend([("/w/home/.probe", 0), ("/w/home/.zshrc", 1)])
    assert events.describe() == "0  /w/home/.probe\n1  /w/home/.zshrc"


@dataclass
class Running:
    """A `Watcher` running in a task, counting its `on_due` calls."""

    events: Events
    source: Path
    home: Path
    targets: list[Path]
    debounce_ms: int = DEBOUNCE
    due: int = 0
    watcher: Watcher = field(init=False)
    task: asyncio.Task[None] = field(init=False)

    def __post_init__(self) -> None:
        self.events.count = self.count
        self.watcher = Watcher(self.source, self.targets, self.debounce_ms, self.on_due)
        self.task = asyncio.ensure_future(self.watcher.run())

    def on_due(self) -> None:
        self.due += 1

    def count(self) -> int:
        return self.due

    async def ready(self) -> None:
        await ready(self.home / ".probe", self.count, self.events, self.debounce_ms)
        self.due = 0

    async def sentinel(self, uncounted: Sequence[Path] = ()) -> None:
        await after_sentinel(
            self.home / ".sentinel", self.events, self.debounce_ms, uncounted=uncounted
        )

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def layout(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    # tmp_path unresolved: on macOS it is behind /var → /private/var.
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    (home / "sub").mkdir(parents=True)
    targets = [home / n for n in (".probe", ".sentinel", ".zshrc")]
    for t in [*targets, home / ".zshrc.bak", home / "sub" / ".zshrc"]:
        t.write_text("start\n")
    return source, home, [*targets, home / "sub" / ".managed-elsewhere"]


Start = Callable[..., Awaitable[Running]]


@pytest.fixture
async def start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Start]:
    """Start a watcher on `layout`'s dirs (`source`, `extra` targets and
    `debounce_ms` changing them) and wait for it to be live; stop it after."""
    events = record_events(monkeypatch)
    made: list[Running] = []

    async def make(
        *, source: Path | None = None, extra: Sequence[Path] = (), debounce_ms: int = DEBOUNCE
    ) -> Running:
        src, home, targets = layout(tmp_path)
        r = Running(events, source or src, home, [*targets, *extra], debounce_ms)
        made.append(r)
        await r.ready()
        return r

    yield make
    for r in made:
        await r.stop()


async def test_a_target_change_is_due_once(start: Start):
    r = await start()
    (r.home / ".zshrc").write_text("changed\n")
    await eventually(lambda: r.due >= 1)
    await r.sentinel()
    assert r.due == 2, r.events.describe()


async def test_a_burst_is_due_once(start: Start):
    r = await start()
    for n in range(5):
        (r.home / ".zshrc").write_text(f"changed {n}\n")
    await eventually(lambda: r.due >= 1)
    await r.sentinel()
    assert r.due == 2, r.events.describe()


async def test_a_source_change_is_due(start: Start):
    r = await start()
    (r.source / "sub").mkdir()
    (r.source / "sub" / "dot_x").write_text("x\n")
    await eventually(lambda: r.due >= 1)


@pytest.mark.parametrize(
    "path",
    [".zshrc.bak", "../source/.git/index", "sub/.zshrc"],
    ids=["unmanaged sibling", "under .git", "subdir of a non-recursive parent"],
)
async def test_changes_that_dont_count(start: Start, path: str):
    r = await start()
    await r.sentinel(uncounted=[r.home / path])
    assert r.due == 1, r.events.describe()


async def test_paused_drops_events_until_resumed(start: Start):
    r = await start()
    r.watcher.pause()
    mark = len(r.events.arrived)
    (r.home / ".zshrc").write_text("while paused\n")
    await eventually(lambda: r.events.saw(".zshrc", mark) is not None)
    await quiet(r.count, DEBOUNCE)
    assert r.due == 0, r.events.describe()
    r.watcher.resume()
    await r.sentinel()
    assert r.due == 1, r.events.describe()


async def test_pause_drops_a_wait_under_way(start: Start):
    r = await start(debounce_ms=300)
    (r.home / ".zshrc").write_text("changed\n")
    await eventually(lambda: r.watcher._timer is not None)
    r.watcher.pause()
    assert r.watcher._timer is None
    r.watcher.resume()
    await r.sentinel()
    assert r.due == 1, r.events.describe()


async def test_zero_debounce_is_due_per_batch(start: Start):
    r = await start(debounce_ms=0)
    (r.home / ".zshrc").write_text("changed\n")
    await eventually(lambda: r.due >= 1)


async def test_a_missing_parent_is_left_out(start: Start, tmp_path: Path):
    r = await start(extra=[tmp_path / "nowhere" / ".x"])
    assert not r.task.done()
    (r.home / ".zshrc").write_text("changed\n")
    await eventually(lambda: r.due >= 1)


async def test_a_missing_source_dir_is_left_out(start: Start, tmp_path: Path):
    r = await start(source=tmp_path / "no-source")
    assert not r.task.done()


async def test_nothing_to_watch_returns(tmp_path: Path):
    watcher = Watcher(tmp_path / "no-source", [tmp_path / "nowhere" / ".x"], DEBOUNCE, lambda: None)
    await asyncio.wait_for(watcher.run(), timeout=5)


async def test_cancelling_returns_promptly_with_nothing_left(start: Start):
    before = asyncio.all_tasks()
    r = await start()
    assert len(asyncio.all_tasks() - before) == 3  # run() and its two watches
    r.task.cancel()
    await asyncio.wait_for(asyncio.gather(r.task, return_exceptions=True), timeout=2)
    assert r.task.cancelled()
    assert asyncio.all_tasks() - before == set()


async def test_an_error_from_watchfiles_propagates(tmp_path: Path, monkeypatch):
    def broken(*paths, **kwargs):
        raise OSError(28, "inotify watch limit reached")

    monkeypatch.setattr(watch, "awatch", broken)
    source, _, targets = layout(tmp_path)
    watcher = Watcher(source, targets, DEBOUNCE, lambda: None)
    with pytest.raises(OSError, match="watch limit"):
        await asyncio.wait_for(watcher.run(), timeout=5)


# --- The watch set's read -------------------------------------------------------------


@requires_chezmoi
async def test_watch_targets_leave_out_scripts(scratch: Scratch):
    h = scratch.home
    (h / ".file").write_text("file\n")
    (h / ".dir").mkdir()
    (h / ".dir" / "inner").write_text("inner\n")
    (h / ".link").symlink_to(".file")
    scratch.run("add", str(h / ".file"), str(h / ".dir"), str(h / ".link"))
    (scratch.source / "run_hello.sh").write_text("#!/bin/sh\necho hello >/dev/null\n")
    targets = await ops.watch_targets(scratch.chezmoi())
    assert targets == (h / ".dir", h / ".dir" / "inner", h / ".file", h / ".link")
