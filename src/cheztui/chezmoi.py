"""The async wrapper around the chezmoi binary (spec §10, DEC-13).

Every chezmoi call goes through `Chezmoi.run`, which puts `--no-tty` and the
`--config`, `--source`, `--destination` and `--dry-run` flags in front of the
command.
"""

from __future__ import annotations

import re
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path

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

    async def _exec(self, argv: list[str]) -> Completed:
        try:
            return await self.runner(argv)
        except FileNotFoundError as e:
            raise ChezmoiUnavailable(f"{self.binary} not found") from e

    async def run(self, *args: str, check: bool = True) -> Completed:
        """Run a chezmoi command; with `check`, a non-zero exit raises `ChezmoiFailed`."""
        done = await self._exec(self.argv(*args))
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
