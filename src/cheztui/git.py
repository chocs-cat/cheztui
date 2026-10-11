"""Read-only queries of the source repo (spec §5 Write flow, §10; DEC-19).

Git runs in chezmoi's `workingTree` with `GIT_OPTIONAL_LOCKS=0`, so a read
never takes the index lock from a chezmoi write. Nothing here fetches.
"""

from __future__ import annotations

import os
from dataclasses import KW_ONLY, dataclass
from pathlib import Path

from cheztui.runner import Completed, Runner, run_subprocess

_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


class GitError(Exception):
    """Base for every git error; a missing binary raises it as is."""


class GitFailed(GitError):
    """A git command exited with a code its caller doesn't expect."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(stderr.strip() or f"git exited {returncode}")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


@dataclass
class Git:
    runner: Runner = run_subprocess
    _: KW_ONLY
    binary: str = "git"
    work_tree: Path

    async def _run(self, *args: str, ok: tuple[int, ...] = (0,)) -> Completed:
        """Run git; an exit code outside `ok` raises `GitFailed`."""
        try:
            done = await self.runner([self.binary, "-C", str(self.work_tree), *args], env=_ENV)
        except FileNotFoundError as e:
            raise GitError(f"{self.binary} not found") from e
        if done.returncode not in ok:
            raise GitFailed(
                done.argv, done.returncode, done.stderr.decode("utf-8", errors="replace")
            )
        return done

    async def porcelain(self) -> list[str]:
        """Paths with uncommitted changes, relative to the work tree.

        A rename or copy gives its new path.
        """
        out = (await self._run("status", "--porcelain=v1", "-z", "--untracked-files=all")).stdout
        records = os.fsdecode(out).split("\0")
        paths = []
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            status, path = record[:2], record[3:]
            paths.append(path)
            if "R" in status or "C" in status:
                i += 1  # the next record is the original path
        return paths

    async def head(self) -> Commit | None:
        """The commit at HEAD, or None before the first commit."""
        if (await self._run("rev-parse", "--verify", "-q", "HEAD", ok=(0, 1))).returncode:
            return None
        out = (await self._run("log", "-1", "--format=%H%x00%s")).stdout.decode()
        sha, _, subject = out.rstrip("\n").partition("\0")
        return Commit(sha, subject)

    async def branch(self) -> str | None:
        """The current branch, or None when HEAD is detached."""
        done = await self._run("symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
        return done.stdout.decode().strip() if done.returncode == 0 else None

    async def _has_upstream(self) -> bool:
        done = await self._run(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", ok=tuple(range(256))
        )
        return done.returncode == 0

    async def ahead_behind(self) -> tuple[int, int] | None:
        """Commits HEAD is ahead of and behind `@{u}`, or None with no upstream or no HEAD."""
        if await self.head() is None or not await self._has_upstream():
            return None
        out = (await self._run("rev-list", "--left-right", "--count", "HEAD...@{u}")).stdout
        ahead, behind = out.decode().split()
        return int(ahead), int(behind)

    async def upstream_contains(self, sha: str) -> bool:
        """Whether `@{u}` contains `sha`: the `pushed` state of DEC-19. False with no upstream."""
        if not await self._has_upstream():
            return False
        done = await self._run("merge-base", "--is-ancestor", sha, "@{u}", ok=(0, 1))
        return done.returncode == 0
