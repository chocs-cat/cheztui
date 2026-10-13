"""Watching (spec §5 *Watching in the TUI*, §10; DEC-37).

`counts` is pure. The `Watcher` tests run real watchfiles on `tmp_path`
dirs, with no chezmoi: negatives use the sentinel pattern in
`tests/watching.py`, never a bare sleep. `watch_targets` runs against the
real chezmoi in the scratch setup.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cheztui import ops, watch
from cheztui.watch import Watcher, counts
from tests.conftest import Scratch, requires_chezmoi
from tests.watching import after_sentinel, eventually, quiet, ready

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


@dataclass
class Running:
    """A `Watcher` running in a task, counting its `on_due` calls."""

    source: Path
    home: Path
    targets: list[Path]
    debounce_ms: int = DEBOUNCE
    due: int = 0
    watcher: Watcher = field(init=False)
    task: asyncio.Task[None] = field(init=False)

    def start(self) -> None:
        self.watcher = Watcher(self.source, self.targets, self.debounce_ms, self.on_due)
        self.task = asyncio.ensure_future(self.watcher.run())

    def on_due(self) -> None:
        self.due += 1

    def count(self) -> int:
        return self.due

    async def ready(self) -> None:
        await ready(self.home / ".probe", self.count, self.debounce_ms)
        self.due = 0

    async def sentinel(self, expected: int) -> None:
        await after_sentinel(self.home / ".sentinel", self.count, expected, self.debounce_ms)


def layout(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    (home / "sub").mkdir(parents=True)
    targets = [home / n for n in (".probe", ".sentinel", ".zshrc")]
    for t in [*targets, home / ".zshrc.bak", home / "sub" / ".zshrc"]:
        t.write_text("start\n")
    return source, home, [*targets, home / "sub" / ".managed-elsewhere"]


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    # tmp_path unresolved: on macOS it is behind /var → /private/var.
    source, home, targets = layout(tmp_path)
    r = Running(source, home, targets)
    r.start()
    await r.ready()
    yield r
    r.task.cancel()
    await asyncio.gather(r.task, return_exceptions=True)


async def test_a_target_change_is_due_once(running: Running):
    (running.home / ".zshrc").write_text("changed\n")
    await eventually(lambda: running.due >= 1)
    await running.sentinel(2)
    assert running.due == 2


async def test_a_burst_is_due_once(running: Running):
    for n in range(5):
        (running.home / ".zshrc").write_text(f"changed {n}\n")
    await eventually(lambda: running.due >= 1)
    await running.sentinel(2)
    assert running.due == 2


async def test_a_source_change_is_due(running: Running):
    (running.source / "sub").mkdir()
    (running.source / "sub" / "dot_x").write_text("x\n")
    await eventually(lambda: running.due >= 1)


@pytest.mark.parametrize(
    "path",
    [".zshrc.bak", "../source/.git/index", "sub/.zshrc"],
    ids=["unmanaged sibling", "under .git", "subdir of a non-recursive parent"],
)
async def test_changes_that_dont_count(running: Running, path: str):
    (running.home / path).write_text("changed\n")
    await running.sentinel(1)
    assert running.due == 1


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path watchfiles reports, paused or not: a dropped event's arrival
    is then something to wait for, not a sleep."""
    paths: list[str] = []
    real = watch.awatch

    async def recording(*args, **kwargs):
        async for changes in real(*args, **kwargs):
            paths.extend(p for _, p in changes)
            yield changes

    monkeypatch.setattr(watch, "awatch", recording)
    return paths


async def test_paused_drops_events_until_resumed(seen: list[str], tmp_path: Path):
    source, home, targets = layout(tmp_path)
    r = Running(source, home, targets)
    r.start()
    try:
        await r.ready()
        r.watcher.pause()
        seen.clear()
        (home / ".zshrc").write_text("while paused\n")
        await eventually(lambda: any(p.endswith("/.zshrc") for p in seen))
        await quiet(r.count, DEBOUNCE)
        assert r.due == 0
        r.watcher.resume()
        await r.sentinel(1)
        assert r.due == 1
    finally:
        r.task.cancel()
        await asyncio.gather(r.task, return_exceptions=True)


async def test_pause_drops_a_wait_under_way(tmp_path: Path):
    source, home, targets = layout(tmp_path)
    r = Running(source, home, targets, debounce_ms=300)
    r.start()
    try:
        await r.ready()
        (home / ".zshrc").write_text("changed\n")
        await eventually(lambda: r.watcher._timer is not None)
        r.watcher.pause()
        assert r.watcher._timer is None
        r.watcher.resume()
        await r.sentinel(1)
        assert r.due == 1
    finally:
        r.task.cancel()
        await asyncio.gather(r.task, return_exceptions=True)


async def test_zero_debounce_is_due_per_batch(tmp_path: Path):
    source, home, targets = layout(tmp_path)
    r = Running(source, home, targets, debounce_ms=0)
    r.start()
    try:
        await r.ready()
        (home / ".zshrc").write_text("changed\n")
        await eventually(lambda: r.due >= 1)
    finally:
        r.task.cancel()
        await asyncio.gather(r.task, return_exceptions=True)


async def test_a_missing_parent_is_left_out(tmp_path: Path):
    source, home, targets = layout(tmp_path)
    r = Running(source, home, [*targets, tmp_path / "nowhere" / ".x"])
    r.start()
    try:
        await r.ready()
        assert not r.task.done()
        (home / ".zshrc").write_text("changed\n")
        await eventually(lambda: r.due >= 1)
    finally:
        r.task.cancel()
        await asyncio.gather(r.task, return_exceptions=True)


async def test_a_missing_source_dir_is_left_out(tmp_path: Path):
    _, home, targets = layout(tmp_path)
    r = Running(tmp_path / "no-source", home, targets)
    r.start()
    try:
        await r.ready()
    finally:
        r.task.cancel()
        await asyncio.gather(r.task, return_exceptions=True)


async def test_nothing_to_watch_returns(tmp_path: Path):
    watcher = Watcher(tmp_path / "no-source", [tmp_path / "nowhere" / ".x"], DEBOUNCE, lambda: None)
    await asyncio.wait_for(watcher.run(), timeout=5)


async def test_cancelling_returns_promptly_with_nothing_left(tmp_path: Path):
    source, home, targets = layout(tmp_path)
    before = asyncio.all_tasks()
    r = Running(source, home, targets)
    r.start()
    await r.ready()
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
