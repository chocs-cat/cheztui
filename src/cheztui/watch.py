"""Filesystem watching for the TUI (spec §5 *Watching in the TUI*, §10; DEC-37).

Knows nothing of Textual or chezmoi: given a source dir, the managed
targets and a debounce, a `Watcher` calls back when a refresh is due, and
can be paused and resumed. The TUI owns what a refresh does and when one
waits.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from watchfiles import awatch


def _resolved(path: str | Path) -> Path:
    """`path` as its parent dir resolved joined with its own name (spec §5 *Which
    events count*): macOS reports resolved paths, and a target may be a symlink."""
    path = Path(path)
    return Path(os.path.realpath(path.parent)) / path.name


def _counts(path: Path, source: Path, targets: frozenset[Path]) -> bool:
    if path.is_relative_to(source):
        return ".git" not in path.relative_to(source).parts
    return path in targets


def counts(path: str | Path, source_dir: Path, targets: Iterable[Path]) -> bool:
    """Whether a change at `path` makes a refresh due (spec §5 *Which events count*).

    A path under the source dir counts unless a `.git` component lies below
    it; elsewhere only a managed target itself counts, not a sibling.
    """
    source = Path(os.path.realpath(source_dir))
    return _counts(_resolved(path), source, frozenset(map(_resolved, targets)))


class Watcher:
    """Watch the source dir and the managed targets; call `on_due` when a refresh is due.

    The source dir is watched recursively and each target's parent dir not
    recursively, with watchfiles' own filter off: `counts` is the only one.
    A counting event restarts a trailing `debounce_ms` wait, and `on_due`
    runs once when it ends; with `debounce_ms == 0` it runs once per batch
    watchfiles reports.
    """

    def __init__(
        self,
        source_dir: Path,
        targets: Sequence[Path],
        debounce_ms: int,
        on_due: Callable[[], None],
    ) -> None:
        self.source_dir = source_dir
        self.targets = tuple(targets)
        self.debounce_ms = debounce_ms
        self.on_due = on_due
        # The source dir resolved whole: everything under it is compared resolved.
        self._source = Path(os.path.realpath(source_dir))
        self._targets = frozenset(map(_resolved, self.targets))
        self._paused = False
        self._timer: asyncio.TimerHandle | None = None

    async def run(self) -> None:
        """Watch until cancelled; return at once with nothing to watch.

        A dir that doesn't exist now is left out (spec §5 *What is watched*).
        An error from watchfiles stops both watches and propagates.
        """
        watches = []
        if self.source_dir.is_dir():
            watches.append(self._watch([self.source_dir], recursive=True))
        parents = sorted({t.parent for t in self.targets if t.parent.is_dir()})
        if parents:
            watches.append(self._watch(parents, recursive=False))
        if not watches:
            return
        tasks = [asyncio.ensure_future(w) for w in watches]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()  # raises the first failure
        finally:
            self._drop_timer()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self, paths: Sequence[Path], *, recursive: bool) -> None:
        async for changes in awatch(*paths, watch_filter=None, recursive=recursive):
            if self._paused:
                continue
            if any(_counts(_resolved(p), self._source, self._targets) for _, p in changes):
                self._due_later()

    def _due_later(self) -> None:
        if self.debounce_ms == 0:
            self.on_due()
            return
        self._drop_timer()
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(self.debounce_ms / 1000, self._fire)

    def _fire(self) -> None:
        self._timer = None
        if not self._paused:
            self.on_due()

    def _drop_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def pause(self) -> None:
        """Drop a wait under way and every event until `resume()` (spec §5
        *cheztui's own writes*)."""
        self._paused = True
        self._drop_timer()

    def resume(self) -> None:
        self._paused = False
