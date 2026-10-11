"""The subprocess runner that `chezmoi.py` and `git.py` share (spec §10).

A runner is an async callable taking an argv and returning a `Completed`.
Tests swap in a fake; the default runs the real binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed: ...


async def run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> Completed:
    """Run `argv` with `env` merged over the process environment.

    A missing binary raises `FileNotFoundError` for the caller to map.
    Cancelling the awaiting task kills the child and waits for it (DEC-6).
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env={**os.environ, **env} if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate(stdin)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    assert proc.returncode is not None
    return Completed(tuple(argv), proc.returncode, stdout, stderr)
