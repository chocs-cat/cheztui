"""The async wrapper around the chezmoi binary (spec §10, DEC-13).

Every chezmoi call goes through `Chezmoi.run`, which puts `--no-tty` and the
`--config`, `--source`, `--destination` and `--dry-run` flags in front of the
command. The parsers turn chezmoi's output into frozen dataclasses; output
they can't read raises `ChezmoiUnavailable`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Any

from cheztui.runner import Completed, Runner, run_subprocess

MIN_VERSION = (2, 60, 0)  # DEC-21; raise CI's CHEZMOI_VERSION with it (DEC-24)

_VERSION_RE = re.compile(r"version v?(\d+)\.(\d+)\.(\d+)")


class ChezmoiError(Exception):
    """Base for every chezmoi error."""


class ChezmoiUnavailable(ChezmoiError):
    """chezmoi is missing, too old, or printed output a parser can't read.

    The CLI reports it as "chezmoi missing or broken" (exit 3, spec §10).
    """


class ChezmoiFailed(ChezmoiError):
    """A chezmoi command exited non-zero."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(self.last_line or f"chezmoi exited {returncode}")

    @property
    def last_line(self) -> str:
        """The last non-empty stderr line, which the log line shows (spec §5 Result)."""
        lines = [line for line in self.stderr.splitlines() if line.strip()]
        return lines[-1].strip() if lines else ""


# --- Parsers ---------------------------------------------------------------


@dataclass(frozen=True)
class StatusRow:
    """One `chezmoi status` line: the two columns and the absolute target."""

    first: str
    second: str
    target: Path

    @property
    def columns(self) -> str:
        return self.first + self.second


@dataclass(frozen=True)
class ManagedEntry:
    relative: str
    absolute: Path
    source_absolute: Path | None
    source_relative: str | None


@dataclass(frozen=True)
class DumpEntry:
    """A target-state entry from `chezmoi dump` (spec §6 Drift, DEC-15)."""

    name: str
    type: str  # chezmoi's own value: file, dir, symlink, script, ...
    perm: int | None
    contents: str | None
    linkname: str | None
    condition: str | None


@dataclass(frozen=True)
class EntryState:
    """What chezmoi recorded when it last wrote a target (DEC-14)."""

    type: str
    mode: int | None
    contents_sha256: str | None


@dataclass(frozen=True)
class ChezmoiConfig:
    source_dir: Path
    working_tree: Path
    dest_dir: Path
    auto_commit: bool  # chezmoi commits writes: git.autocommit or git.autopush (DEC-23)
    auto_push: bool
    edit_command: str = ""


def parse_status(text: str) -> list[StatusRow]:
    """Parse `chezmoi status --path-style=absolute`.

    Lines split on newlines only, never `splitlines()`, and the path is
    everything after the two columns and the space, unstripped: a name may
    hold spaces, carriage returns or other separators.
    """
    rows = []
    for line in text.split("\n"):
        if not line:
            continue
        if len(line) < 4 or line[2] != " ":
            raise ChezmoiUnavailable(f"can't read a line of chezmoi status: {line!r}")
        rows.append(StatusRow(line[0], line[1], Path(line[3:])))
    return rows


def _json_object(text: str, command: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ChezmoiUnavailable(f"chezmoi {command} printed invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ChezmoiUnavailable(f"chezmoi {command} printed JSON that isn't an object")
    return data


class _Fields:
    """Typed access to one JSON object, raising `ChezmoiUnavailable` naming the command."""

    def __init__(self, data: object, command: str, where: str) -> None:
        if not isinstance(data, dict):
            raise ChezmoiUnavailable(f"chezmoi {command}: {where} isn't an object")
        self.data, self.command, self.where = data, command, where

    def _get(self, key: str, kind: type, required: bool) -> Any:
        value = self.data.get(key)
        if value is None:
            if required:
                raise ChezmoiUnavailable(f"chezmoi {self.command}: {self.where} has no {key!r}")
            return None
        # bool is an int to isinstance, and never a valid int here.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ChezmoiUnavailable(
                f"chezmoi {self.command}: {self.where}: {key!r} isn't a {kind.__name__}"
            )
        return value

    def text(self, key: str) -> str:
        return self._get(key, str, True)

    def opt_text(self, key: str) -> str | None:
        return self._get(key, str, False)

    def opt_int(self, key: str) -> int | None:
        return self._get(key, int, False)

    def flag(self, key: str) -> bool:
        return self._get(key, bool, True)

    def obj(self, key: str) -> _Fields:
        return _Fields(self._get(key, dict, True), self.command, f"{self.where}.{key}")


def parse_managed(text: str) -> dict[str, ManagedEntry]:
    """Parse `chezmoi managed --format=json --path-style=all`, keyed by relative target."""
    command = "managed"
    entries = {}
    for relative, value in _json_object(text, command).items():
        f = _Fields(value, command, repr(relative))
        source_absolute = f.opt_text("sourceAbsolute")
        entries[relative] = ManagedEntry(
            relative=relative,
            absolute=Path(f.text("absolute")),
            source_absolute=Path(source_absolute) if source_absolute is not None else None,
            source_relative=f.opt_text("sourceRelative"),
        )
    return entries


def parse_dump(text: str) -> dict[str, DumpEntry]:
    """Parse `chezmoi dump --format=json`, keyed by relative target."""
    command = "dump"
    entries = {}
    for relative, value in _json_object(text, command).items():
        f = _Fields(value, command, repr(relative))
        entries[relative] = DumpEntry(
            name=f.text("name"),
            type=f.text("type"),
            perm=f.opt_int("perm"),
            contents=f.opt_text("contents"),
            linkname=f.opt_text("linkname"),
            condition=f.opt_text("condition"),
        )
    return entries


def parse_state_dump(text: str) -> dict[Path, EntryState]:
    """Parse the `entryState` bucket of `chezmoi state dump --format=json`."""
    command = "state dump"
    top = _Fields(_json_object(text, command), command, "the output")
    entries = {}
    for target, value in top.obj("entryState").data.items():
        f = _Fields(value, command, repr(target))
        entries[Path(target)] = EntryState(
            type=f.text("type"),
            mode=f.opt_int("mode"),
            contents_sha256=f.opt_text("contentsSHA256"),
        )
    return entries


def parse_dump_config(text: str) -> ChezmoiConfig:
    """Parse `chezmoi dump-config --format=json`, which spells the git keys in lowercase."""
    command = "dump-config"
    f = _Fields(_json_object(text, command), command, "the output")
    git = f.obj("git")
    auto_push = git.flag("autopush")
    return ChezmoiConfig(
        source_dir=Path(f.text("sourceDir")),
        working_tree=Path(f.text("workingTree")),
        dest_dir=Path(f.text("destDir")),
        auto_commit=git.flag("autocommit") or auto_push,  # DEC-23
        auto_push=auto_push,
        edit_command=_Fields(f.data.get("edit", {}), command, "edit").opt_text("command") or "",
    )


# --- The wrapper -------------------------------------------------------------


@dataclass
class Chezmoi:
    runner: Runner = run_subprocess
    _: KW_ONLY
    binary: str = "chezmoi"
    config: Path | None = None
    source: Path | None = None
    destination: Path | None = None
    dry_run: bool = False
    _version: tuple[int, int, int] | None = field(default=None, init=False, repr=False)
    _version_checked: bool = field(default=False, init=False, repr=False)

    def argv(self, *args: str) -> list[str]:
        """The full argv for a chezmoi command (spec §10's flag order)."""
        argv = [self.binary, "--no-tty"]
        if self.config is not None:
            argv += ["--config", str(self.config)]
        if self.source is not None:
            argv += ["--source", str(self.source)]
        if self.destination is not None:
            argv += ["--destination", str(self.destination)]
        if self.dry_run:
            argv.append("--dry-run")
        argv += args
        return argv

    async def _exec(
        self,
        argv: list[str],
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        try:
            return await (runner or self.runner)(argv, env=env)
        except FileNotFoundError as e:
            raise ChezmoiUnavailable(f"{self.binary} not found") from e

    async def run(
        self,
        *args: str,
        check: bool = True,
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        """Run a chezmoi command; with `check`, a non-zero exit raises `ChezmoiFailed`.

        `runner` replaces the instance's for this one call, as a front end's
        terminal runner does for an interactive write, and `env` is merged
        over the process environment (spec §10). The argv is the same.
        """
        done = await self._exec(self.argv(*args), runner, env)
        if check and done.returncode != 0:
            raise ChezmoiFailed(
                done.argv, done.returncode, done.stderr.decode("utf-8", errors="replace")
            )
        return done

    async def check_version(self) -> tuple[int, int, int] | None:
        """Check chezmoi is at least `MIN_VERSION`, once per instance (DEC-21).

        Returns the version, or None when it isn't `vX.Y.Z` (a development
        build prints `dev`), which passes.
        """
        if not self._version_checked:
            done = await self._exec([self.binary, "--version"])
            if done.returncode != 0:
                raise ChezmoiUnavailable(f"{self.binary} --version exited {done.returncode}")
            m = _VERSION_RE.search(done.stdout.decode("utf-8", errors="replace"))
            self._version = (int(m[1]), int(m[2]), int(m[3])) if m else None
            self._version_checked = True
        if self._version is not None and self._version < MIN_VERSION:
            found = ".".join(map(str, self._version))
            required = ".".join(map(str, MIN_VERSION))
            raise ChezmoiUnavailable(
                f"chezmoi {found} is too old: cheztui needs {required} or later"
            )
        return self._version

    async def _text(self, *args: str) -> str:
        # Paths may not be UTF-8; surrogateescape keeps them round-tripping to Path.
        return os.fsdecode((await self.run(*args)).stdout)

    async def status(self, targets: Sequence[str | Path] = ()) -> list[StatusRow]:
        return parse_status(await self._text("status", "--path-style=absolute", *map(str, targets)))

    async def managed(
        self,
        include: Sequence[str] = (),
        targets: Sequence[str | Path] = (),
        *,
        exclude: Sequence[str] = (),
    ) -> dict[str, ManagedEntry]:
        args = ["managed", "--format=json", "--path-style=all"]
        if include:
            args.append(f"--include={','.join(include)}")
        if exclude:
            args.append(f"--exclude={','.join(exclude)}")
        return parse_managed(await self._text(*args, *map(str, targets)))

    async def dump(self, targets: Sequence[str | Path] = ()) -> dict[str, DumpEntry]:
        return parse_dump(await self._text("dump", "--format=json", *map(str, targets)))

    async def cat(self, target: str | Path) -> bytes:
        """The target state's contents as chezmoi would write them, bytes unchanged."""
        return (await self.run("cat", str(target))).stdout

    async def diff(self, target: str | Path) -> str:
        """chezmoi's built-in diff for one target, no pager or colour (spec §6 Drift, DEC-34)."""
        done = await self.run(
            "diff", "--no-pager", "--color=false", "--use-builtin-diff", str(target)
        )
        return done.stdout.decode("utf-8", errors="replace")

    async def entry_state(self) -> dict[Path, EntryState]:
        return parse_state_dump(await self._text("state", "dump", "--format=json"))

    async def dump_config(self) -> ChezmoiConfig:
        return parse_dump_config(await self._text("dump-config", "--format=json"))

    async def source_text(self, source: Path) -> str:
        """A source file's literal contents, read through chezmoi (spec §5, DEC-13).

        `include` returns the file unrendered, and `execute-template` renders
        only the one-action template around it. For password-manager call
        detection: keep the text transient.
        """
        return (await self.source_bytes(source)).decode("utf-8", errors="replace")

    async def source_bytes(self, source: Path) -> bytes:
        """Literal source bytes, preserving UTF-8 and line endings (spec §8, DEC-31)."""
        template = "{{ include " + go_quote(source) + " }}"
        return (await self.run("execute-template", template)).stdout

    async def decrypt(self, source: Path) -> bytes:
        return (await self.run("decrypt", str(source))).stdout

    async def verify(self, target: Path) -> bool:
        return (await self.run("verify", str(target), check=False)).returncode == 0


def go_quote(path: Path) -> str:
    """A Go string literal holding `path`'s bytes: printable ASCII as is, else `\\xNN`."""
    out = []
    for byte in os.fsencode(path):
        char = chr(byte)
        if char in '"\\':
            out.append("\\" + char)
        elif 0x20 <= byte < 0x7F:
            out.append(char)
        else:
            out.append(f"\\x{byte:02x}")
    return '"' + "".join(out) + '"'
