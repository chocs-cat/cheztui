"""Fake chezmoi and git runners for the write path (spec §5 Write flow, §11).

One `FakeWrite` answers both binaries and keeps one log of every call, so
tests can assert ordering: no writing chezmoi command, dry run included,
before a passing pre-flight. Unit tests only; integration tests use the
scratch setup and the real binaries.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cheztui.chezmoi import Chezmoi
from cheztui.config import Config
from cheztui.ops import WriteOps
from cheztui.runner import Completed

H = Path("/h")  # destination
S = Path("/s")  # source dir and working tree

# chezmoi's read-only commands; anything else counts as a write.
READS = frozenset(
    {"dump-config", "managed", "execute-template", "status", "dump", "cat", "decrypt", "verify"}
)

_VALUE_FLAGS = frozenset({"--config", "--source", "--destination"})


def subcommand(argv: Sequence[str]) -> tuple[str, ...]:
    """The chezmoi argv after the global flags: the command and its own arguments."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _VALUE_FLAGS else 1
    return tuple(argv[i:])


def is_write(call: tuple[str, ...]) -> bool:
    if call[0] != "chezmoi":
        return False
    words = subcommand(call)
    # `--version` has no subcommand; `state dump` reads where `state delete` writes.
    return bool(words) and words[0] not in READS and words[:2] != ("state", "dump")


Reply = Callable[[tuple[str, ...], Mapping[str, str] | None], Completed | None]


@dataclass
class FakeWrite:
    source: Path = S
    dest: Path = H
    work_tree: Path = S
    auto_commit: bool = False
    auto_push: bool = False
    stray: list[str] = field(default_factory=list)
    head: tuple[str, str] | None = ("a" * 40, "initial")
    upstream: set[str] | None = None  # SHAs `@{u}` contains; None: no upstream
    log: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[Mapping[str, str] | None] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)  # tried first, newest first
    holds: dict[str, asyncio.Event] = field(default_factory=dict)

    def reply(self, reply: Reply) -> None:
        self.replies.insert(0, reply)

    def hold(self, command: str) -> asyncio.Event:
        """Make every chezmoi `command` (or git subcommand) wait for the returned event.

        The call is logged when it starts; `--version` names the version check.
        """
        event = self.holds[command] = asyncio.Event()
        return event

    async def _wait(self, call: tuple[str, ...]) -> None:
        # `chezmoi --version` has no subcommand; git's follows `-C <work tree>`.
        words = (subcommand(call) or call[1:2]) if call[0] == "chezmoi" else call[3:]
        if words and words[0] in self.holds:
            await self.holds[words[0]].wait()

    def fail(
        self, command: str, stderr: bytes, returncode: int = 1, *, binary: str | None = None
    ) -> None:
        """Make every chezmoi `command` (or git subcommand) exit non-zero.

        `binary` (`chezmoi` or `git`) limits it to one, where both have the command.
        """

        def reply(argv: tuple[str, ...], env: Mapping[str, str] | None) -> Completed | None:
            words = subcommand(argv) if argv[0] == "chezmoi" else argv[3:]
            if binary not in (None, argv[0]):
                return None
            if words and words[0] == command:
                return Completed(argv, returncode, b"", stderr)
            return None

        self.reply(reply)

    def commit(self, subject: str) -> None:
        """Move HEAD, as an auto-committing write would."""
        sha = format(len(self.log) + 1, "x").rjust(40, "b")
        self.head = (sha, subject)

    async def chezmoi_runner(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed:
        call = tuple(argv)
        self.log.append(call)
        self.envs.append(env)
        await self._wait(call)
        for reply in self.replies:
            done = reply(call, env)
            if done is not None:
                return done
        return self.chezmoi_default(call)

    def chezmoi_default(self, call: tuple[str, ...]) -> Completed:
        """The answer when no reply claims a chezmoi call; subclasses add commands."""
        if subcommand(call)[:1] == ("dump-config",):
            config = {
                "sourceDir": str(self.source),
                "workingTree": str(self.work_tree),
                "destDir": str(self.dest),
                "git": {"autocommit": self.auto_commit, "autopush": self.auto_push},
            }
            return Completed(call, 0, json.dumps(config).encode(), b"")
        return Completed(call, 0, b"", b"")

    async def git_runner(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed:
        call = tuple(argv)
        self.log.append(call)
        await self._wait(call)
        for reply in self.replies:
            done = reply(call, env)
            if done is not None:
                return done
        return self.git_default(call)

    def git_default(self, call: tuple[str, ...]) -> Completed:
        """The answer when no reply claims a git call; subclasses add commands."""
        args = call[3:]  # git -C <work tree> ...
        ok = Completed(call, 0, b"", b"")
        match args:
            case ("status", *_):
                out = b"".join(b"?? " + p.encode() + b"\0" for p in self.stray)
                return Completed(call, 0, out, b"")
            case ("rev-parse", "--verify", "-q", "HEAD"):
                return ok if self.head else Completed(call, 1, b"", b"")
            case ("log", *_):
                assert self.head is not None
                return Completed(call, 0, f"{self.head[0]}\0{self.head[1]}\n".encode(), b"")
            case ("rev-parse", "--abbrev-ref", *_):
                return ok if self.upstream is not None else Completed(call, 128, b"", b"")
            case ("merge-base", "--is-ancestor", sha, _):
                return ok if sha in (self.upstream or ()) else Completed(call, 1, b"", b"")
        raise AssertionError(f"unexpected git call {call}")

    def chezmoi(self, **flags) -> Chezmoi:
        return Chezmoi(self.chezmoi_runner, **flags)

    def ops(self, settings: Config | None = None, **flags) -> WriteOps:
        """A `WriteOps` over both fakes; `flags` go to `Chezmoi`, `settings` is cheztui's config."""
        return WriteOps(
            self.chezmoi(**flags),
            settings if settings is not None else Config(Path("/c/config.toml")),
            git_runner=self.git_runner,
        )

    @property
    def chezmoi_calls(self) -> list[tuple[str, ...]]:
        return [subcommand(c) for c in self.log if c[0] == "chezmoi"]

    @property
    def writes(self) -> list[tuple[str, ...]]:
        return [subcommand(c) for c in self.log if is_write(c)]
