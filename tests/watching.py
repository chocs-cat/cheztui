"""Helpers for tests that wait on real filesystem events (spec §5 *Watching in the TUI*).

Events are slow and unordered, on macOS CI most of all, so no test sleeps a
fixed time: positives poll for their condition, and a negative ("this
didn't refresh") is shown by changing a sentinel afterwards and waiting for
its one refresh, so the count is checked once everything before it has
had its chance to arrive.

`Watchers` makes `FakeWatcher`s; conftest installs it as the default
watcher factory, so a TUI test watches nothing real unless it asks to.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path

TIMEOUT = 15.0  # seconds; generous for macOS CI


async def eventually(condition: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    """Poll `condition` until it holds; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.02)


async def quiet(count: Callable[[], int], debounce_ms: int) -> None:
    """Wait until `count` has stayed the same for two debounces."""
    window = 2 * max(debounce_ms, 50) / 1000
    deadline = time.monotonic() + TIMEOUT
    while True:
        before = count()
        await asyncio.sleep(window)
        if count() == before:
            return
        if time.monotonic() > deadline:
            raise AssertionError("the count never settled")


async def ready(probe: Path, count: Callable[[], int], debounce_ms: int) -> None:
    """Wait for watching to be live: watchfiles has no "watching now" signal.

    Touch `probe` (a watched path) until `count` moves, then wait for quiet.
    The caller resets its count afterwards.
    """
    deadline = time.monotonic() + TIMEOUT
    start = count()
    n = 0
    while count() == start:
        if time.monotonic() > deadline:
            raise AssertionError("watching never started")
        n += 1
        probe.write_text(f"probe {n}\n")
        # Long enough for the debounce to end before the next probe restarts it.
        wait = time.monotonic() + max(0.5, 3 * debounce_ms / 1000)
        while count() == start and time.monotonic() < wait:
            await asyncio.sleep(0.02)
    await quiet(count, debounce_ms)


async def after_sentinel(
    sentinel: Path, count: Callable[[], int], expected: int, debounce_ms: int
) -> None:
    """Change `sentinel` and wait for the count to reach `expected`, then for
    quiet, so the caller's exact assertion catches a late extra."""
    sentinel.write_text(f"sentinel {time.monotonic()}\n")
    await eventually(lambda: count() >= expected)
    await quiet(count, debounce_ms)


class FakeWatcher:
    """A watcher that watches nothing: `due()` calls back as a due refresh would,
    `fail()` ends `run()` with an error, and `pause`/`resume` are recorded."""

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
        self.calls: list[str] = []  # "pause" and "resume", in order
        self.running = False
        self.stopped = False  # run() was cancelled
        self._end: asyncio.Future[None] | None = None

    @property
    def paused(self) -> bool:
        return bool(self.calls) and self.calls[-1] == "pause"

    async def run(self) -> None:
        self._end = asyncio.get_running_loop().create_future()
        self.running = True
        try:
            await self._end
        except asyncio.CancelledError:
            self.stopped = True
            raise
        finally:
            self.running = False

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def due(self) -> None:
        self.on_due()

    def fail(self, error: Exception) -> None:
        assert self._end is not None
        self._end.set_exception(error)


class Watchers:
    """A watcher factory that makes `FakeWatcher`s and keeps them, newest last."""

    def __init__(self) -> None:
        self.made: list[FakeWatcher] = []

    def __call__(
        self,
        source_dir: Path,
        targets: Sequence[Path],
        debounce_ms: int,
        on_due: Callable[[], None],
    ) -> FakeWatcher:
        watcher = FakeWatcher(source_dir, targets, debounce_ms, on_due)
        self.made.append(watcher)
        return watcher

    @property
    def current(self) -> FakeWatcher:
        return self.made[-1]
