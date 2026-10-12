"""A fake chezmoi and git for TUI tests (spec §11, Fake).

`FakeChezmoi` extends the write path's `FakeWrite`, so one fake answers
chezmoi and git with one call log. It adds the reads: `--version`, `status`,
`managed` with and without each `--include`, `dump`, `state dump` and `cat`
from a canned state, and git's branch and ahead/behind. `drifted()` replays
the recorded fixtures in `tests/fixtures/chezmoi/`, so it can't drift from
what real chezmoi prints. Failures use `FakeWrite.fail`, and `FakeWrite.hold`
makes a call wait on an event.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from cheztui.runner import Completed
from tests.fake_write import FakeWrite, subcommand
from tests.test_chezmoi_reads import ROOT, fixture

VERSION = "chezmoi version v2.60.0, commit 0123456789abcdef, built at 2025-01-01T00:00:00Z"


@dataclass
class FakeChezmoi(FakeWrite):
    version: str | None = VERSION  # None: the binary is missing
    status: str = ""
    managed: dict[str | None, str] = field(default_factory=dict)  # by `--include` value
    dump: str = "{}"
    state: str = '{"entryState": {}}'
    cat: dict[Path, bytes] = field(default_factory=dict)
    branch: str | None = "master"  # None: HEAD is detached
    counts: tuple[int, int] = (0, 0)  # ahead, behind; with an upstream only

    @classmethod
    def in_sync(cls, **kwargs) -> FakeChezmoi:
        return cls(**kwargs)

    @classmethod
    def drifted(cls, **kwargs) -> FakeChezmoi:
        """The recorded fixtures, with `/scratch` paths in `dump-config` to match them."""
        fake = cls(
            source=ROOT / "source",
            dest=ROOT / "home",
            work_tree=ROOT / "source",
            status=fixture("status.txt"),
            managed={
                None: fixture("managed.json"),
                "templates": fixture("managed-templates.json"),
                "encrypted": "{}",
                "externals": "{}",
            },
            dump=fixture("dump.json"),
            state=fixture("state-dump.json"),
        )
        return replace(fake, **kwargs)

    def chezmoi_default(self, call: tuple[str, ...]) -> Completed:
        if call[1:] == ("--version",):
            if self.version is None:
                raise FileNotFoundError(call[0])
            return Completed(call, 0, f"{self.version}\n".encode(), b"")
        words = subcommand(call)
        out: str | bytes | None = None
        match words:
            case ("status", *_):
                out = self.status
            case ("managed", *args):
                include = next(
                    (a.split("=", 1)[1] for a in args if a.startswith("--include=")), None
                )
                out = self.managed.get(include, "{}")
            case ("dump", *_):
                out = self.dump
            case ("state", "dump", *_):
                out = self.state
            case ("cat", target):
                out = self.cat.get(Path(target), b"")
        if out is None:
            return super().chezmoi_default(call)
        return Completed(call, 0, out.encode() if isinstance(out, str) else out, b"")

    def git_default(self, call: tuple[str, ...]) -> Completed:
        match call[3:]:
            case ("symbolic-ref", *_):
                if self.branch is None:
                    return Completed(call, 1, b"", b"")
                return Completed(call, 0, f"{self.branch}\n".encode(), b"")
            case ("rev-list", "--left-right", "--count", _):
                return Completed(call, 0, "{}\t{}\n".format(*self.counts).encode(), b"")
        return super().git_default(call)
