"""cheztui's behaviour, shared by the CLI and the TUI (spec §7, §10).

The Drift model (spec §6 Drift; DEC-14, DEC-15): `drift()` reads chezmoi
and `build_drift()` turns the reads into rows. `build_drift` does no I/O,
so the unit tests and the TUI's fake runner share it.

The write path (spec §5 Write flow; DEC-8, DEC-27 to DEC-30): `WriteOps`
runs the pre-flight, prepares masked plans and executes them, returning
dataclasses that hold only masked text.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from cheztui.chezmoi import (
    Chezmoi,
    ChezmoiFailed,
    DumpEntry,
    EntryState,
    ManagedEntry,
    StatusRow,
)
from cheztui.config import Config, SecretsConfig
from cheztui.git import Commit, Git, GitFailed
from cheztui.runner import Runner, run_subprocess
from cheztui.secrets import Masker

Direction = Literal["you", "src", "both", "run"]
Fix = Literal["apply", "re-add", "merge"]

# The Drift view's words for each direction (spec §4, §6).
DIRECTION_LABELS: dict[Direction, str] = {
    "both": "both",
    "you": "you→",
    "src": "src→",
    "run": "run",
}

_DIRECTION_ORDER: dict[Direction, int] = {"both": 0, "you": 1, "src": 2, "run": 3}
_TAG_ORDER = ("tmpl", "enc", "exec", "link", "ext", "dir")


@dataclass(frozen=True)
class DriftRow:
    target: Path
    source: Path | None
    direction: Direction
    default_fix: Fix
    columns: str
    tags: tuple[str, ...]
    kind: str | None  # the target-state `type` from `dump`; None with no entry
    fix_overwrites: bool  # the default fix overwrites the user's change (DEC-14)


@dataclass(frozen=True)
class Drift:
    rows: tuple[DriftRow, ...] = ()

    @property
    def verified(self) -> bool:
        """No row has its second status column set (spec §4, DEC-19)."""
        return not self.rows

    @property
    def failing(self) -> tuple[Path, ...]:
        """Every row's target, in the Drift view's sort order (spec §7)."""
        return tuple(row.target for row in self.rows)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def needs_cat(row: StatusRow, entry: DumpEntry | None) -> bool:
    """A file row compared by contents whose dump lost bytes to U+FFFD (spec §6, #11)."""
    return (
        row.first != " "
        and row.second not in (" ", "R")
        and entry is not None
        and entry.type == "file"
        and "�" in (entry.contents or "")
    )


def _unchanged_in_source(
    entry: DumpEntry | None, state: EntryState | None, raw: bytes | None
) -> bool:
    """Whether the target state still matches what chezmoi last wrote (DEC-14, per type)."""
    if entry is None or state is None or state.type != entry.type:
        return False
    match entry.type:
        case "file":
            data = raw if raw is not None else (entry.contents or "").encode()
            return _sha256(data) == state.contents_sha256
        case "symlink":
            return _sha256((entry.linkname or "").encode()) == state.contents_sha256
        case "dir":
            return (
                entry.perm is not None
                and state.mode is not None
                and entry.perm == state.mode & 0o777
            )
        case _:
            # dump gives only file, dir, symlink and script, and a script row
            # is `run`; anything else has nothing spec §6 says to compare.
            return False


def _direction(
    row: StatusRow, entry: DumpEntry | None, state: EntryState | None, raw: bytes | None
) -> Direction:
    if row.second == "R":
        return "run"
    if row.first == " ":
        return "src"
    return "you" if _unchanged_in_source(entry, state, raw) else "both"


def _default_fix(
    row: StatusRow, direction: Direction, kind: str | None, tags: tuple[str, ...]
) -> tuple[Fix, bool]:
    """The first line of spec §6's default-fix table that matches, and whether it overwrites."""
    if direction in ("run", "src") or row.first == "D":
        return "apply", False
    if direction == "you" and "tmpl" in tags:
        return "merge", False
    if kind in ("dir", "symlink", None):
        return "apply", True
    return ("re-add" if direction == "you" else "merge"), False


def _tags(
    relative: str | None,
    entry: DumpEntry | None,
    templates: Mapping[str, ManagedEntry],
    encrypted: Mapping[str, ManagedEntry],
    externals: Mapping[str, ManagedEntry],
) -> tuple[str, ...]:
    """Row tags from `managed --include` and `dump`, never from source names (DEC-15)."""
    tags = set()
    if relative is not None:
        tags |= {
            tag
            for tag, entries in (("tmpl", templates), ("enc", encrypted), ("ext", externals))
            if relative in entries
        }
    if entry is not None:
        if entry.type == "dir":
            tags.add("dir")
        elif entry.type == "symlink":
            tags.add("link")
        elif entry.type == "file" and entry.perm is not None and entry.perm & 0o111:
            tags.add("exec")
    return tuple(tag for tag in _TAG_ORDER if tag in tags)


def build_drift(
    status: Sequence[StatusRow],
    managed: Mapping[str, ManagedEntry],
    templates: Mapping[str, ManagedEntry],
    encrypted: Mapping[str, ManagedEntry],
    externals: Mapping[str, ManagedEntry],
    dump: Mapping[str, DumpEntry],
    entry_state: Mapping[Path, EntryState],
    raw: Mapping[Path, bytes] | None = None,
) -> Drift:
    """Turn chezmoi's reads into Drift rows (spec §6 Drift).

    `managed` and the `--include` lists and `dump` are keyed by the relative
    target, `status` and `entry_state` by the absolute one; `managed` joins
    them. `raw` holds `chezmoi cat`'s bytes for the rows `needs_cat` names.
    """
    raw = raw or {}
    by_absolute = {m.absolute: m for m in managed.values()}
    rows = []
    for row in status:
        if row.second == " ":
            continue
        m = by_absolute.get(row.target)
        relative = m.relative if m is not None else None
        entry = dump.get(relative) if relative is not None else None
        direction = _direction(row, entry, entry_state.get(row.target), raw.get(row.target))
        tags = _tags(relative, entry, templates, encrypted, externals)
        kind = entry.type if entry is not None else None
        fix, overwrites = _default_fix(row, direction, kind, tags)
        rows.append(
            DriftRow(
                target=row.target,
                # A `.chezmoiremove` target is in `managed` with no source path.
                source=m.source_absolute if m is not None and m.source_relative else None,
                direction=direction,
                default_fix=fix,
                columns=row.columns,
                tags=tags,
                kind=kind,
                fix_overwrites=overwrites,
            )
        )
    rows.sort(key=lambda r: (_DIRECTION_ORDER[r.direction], str(r.target)))
    return Drift(tuple(rows))


async def drift(chezmoi: Chezmoi) -> Drift:
    """Read chezmoi and build the Drift model; with no drift, `status` is the only call."""
    status = [row for row in await chezmoi.status() if row.second != " "]
    if not status:
        return Drift()
    targets = [row.target for row in status]
    managed, templates, encrypted, externals, dump, entry_state = await asyncio.gather(
        chezmoi.managed(),
        chezmoi.managed(["templates"]),
        chezmoi.managed(["encrypted"]),
        chezmoi.managed(["externals"]),
        chezmoi.dump(targets),
        chezmoi.entry_state(),
    )
    by_absolute = {m.absolute: m.relative for m in managed.values()}
    to_cat = [
        row.target
        for row in status
        if row.target in by_absolute and needs_cat(row, dump.get(by_absolute[row.target]))
    ]
    raw = dict(zip(to_cat, await asyncio.gather(*map(chezmoi.cat, to_cat)), strict=True))
    return build_drift(status, managed, templates, encrypted, externals, dump, entry_state, raw)


# --- The write path (spec §5 Write flow; DEC-8, DEC-27 to DEC-30) ------------

Action = Literal["apply", "re-add", "forget", "edit", "merge"]

# The arguments after the global flags and before the targets (spec §5 Drift
# command plans, DEC-16, DEC-28).
ACTION_ARGS: dict[Action, tuple[str, ...]] = {
    "apply": ("apply", "--force", "--recursive=false"),
    "re-add": ("re-add", "--recursive=false"),
    "forget": ("forget", "--force"),
    "edit": ("edit", "--apply"),
    "merge": ("merge",),
}
INTERACTIVE: frozenset[Action] = frozenset({"edit", "merge"})  # DEC-4
PREVIEW_FLAGS = ("--dry-run", "--verbose", "--no-pager", "--color=false", "--use-builtin-diff")


@dataclass(frozen=True)
class Preflight:
    """The effective chezmoi paths and the source tree's state (spec §5 Pre-flight; DEC-8)."""

    source_dir: Path
    dest_dir: Path
    working_tree: Path
    auto_commit: bool  # git.autocommit or git.autopush (DEC-23)
    stray: tuple[str, ...]  # uncommitted paths, relative to the working tree

    @property
    def blocked(self) -> bool:
        """chezmoi would sweep the stray files into its commit."""
        return self.auto_commit and bool(self.stray)

    @property
    def warns(self) -> bool:
        """Stray files are listed as a warning, not a block (DEC-8)."""
        return not self.auto_commit and bool(self.stray)

    def same_paths(self, other: Preflight) -> bool:
        """A changed source, destination or working tree needs a new plan (DEC-27)."""
        return (self.source_dir, self.dest_dir, self.working_tree) == (
            other.source_dir,
            other.dest_dir,
            other.working_tree,
        )


@dataclass(frozen=True)
class WriteCommand:
    """One chezmoi call: exact argv, run without a shell (spec §5 Preparation and execution)."""

    action: Action
    targets: tuple[Path, ...]
    argv: tuple[str, ...]  # the full argv, global flags included
    interactive: bool

    @property
    def display(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class WriteEntry:
    """One target in a plan, with its masked preview (spec §5 Drift command plans)."""

    target: Path
    source: Path | None
    action: Action
    selected: bool
    diff: str | None = None  # masked dry-run output; None when not previewed
    masking: tuple[str, ...] = ()  # whole-masking reasons: encrypted, always_mask, secret_template
    overwrites: bool = False  # applying it overwrites the user's change (DEC-16)
    excluded: str | None = None  # why the action can't take it
    implied_by: Path | None = None  # the forgotten directory this descendant goes with
    direction: Direction | None = None


@dataclass(frozen=True)
class WritePlan:
    """A prepared write: data for a preview, never authorization to skip pre-flight (DEC-27)."""

    preflight: Preflight
    entries: tuple[WriteEntry, ...] = ()
    commands: tuple[WriteCommand, ...] = ()
    error: str | None = None  # masked short error: a preview failed
    diagnostic: str = ""  # its full masked output
    apply_all: bool = False
    prefix: tuple[str, ...] = field(default=(), repr=False)  # chezmoi's argv before the command

    @property
    def blocked(self) -> bool:
        return self.preflight.blocked or self.error is not None

    @property
    def needs_confirmation(self) -> bool:
        """Interactive-only plans open no modal (DEC-4)."""
        return any(not c.interactive for c in self.commands)


Outcome = Literal["ok", "failed", "refused", "dry run"]
CommitState = Literal["committed", "not committed", "no commit", "dry run"]


@dataclass(frozen=True)
class CommandResult:
    """What one attempted command did (spec §5 Preparation and execution; DEC-19, DEC-27)."""

    command: WriteCommand
    outcome: Outcome
    returncode: int | None = None  # None when it didn't run
    stdout: str = ""  # masked
    stderr: str = ""  # masked, all of it
    error: str | None = None  # masked short error
    commit: Commit | None = None
    pushed: bool | None = None
    state: CommitState | None = None
    stray: tuple[str, ...] = ()  # uncommitted paths after the command, or the refusal's
    reporting_error: str | None = None  # the command ran, then reading its result failed


@dataclass(frozen=True)
class WriteResult:
    """Every command result in execution order, and the commands left unrun (DEC-27)."""

    results: tuple[CommandResult, ...] = ()
    unrun: tuple[WriteCommand, ...] = ()
    refused: str | None = None  # why nothing ran
    warning: tuple[str, ...] = ()  # stray files with auto-commit off (DEC-8)
    dry_run: bool = False
    refresh: bool = False  # a command was attempted: the front end refreshes

    @property
    def ok(self) -> bool:
        return self.refused is None and all(r.outcome in ("ok", "dry run") for r in self.results)


_T = TypeVar("_T")


def _whole_masker() -> Masker:
    # Whole masking needs no destination: the globs never decide it.
    return Masker(SecretsConfig(), Path("/"))


def mask_diagnostic(masker: Masker | None, text: str, *, whole: bool) -> str:
    """Mask command output that can't be assigned to one target (spec §5 Diagnostics, DEC-30).

    Without a masker the masking metadata is unknown, so the text is masked whole.
    """
    if masker is None:
        masker, whole = _whole_masker(), True
    return masker.mask(text, Path("/"), whole=whole, never_mask=False).text


def short_error(stderr: str, action: str, returncode: int) -> str:
    """The original last stderr line, or `chezmoi <action> exited <code>` (spec §5)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"chezmoi {action} exited {returncode}"


async def sanitized(awaitable: Awaitable[_T], masker: Masker | None, *, whole: bool) -> _T:
    """Await a read; a chezmoi or git failure re-raises with its stderr masked (DEC-30).

    The raise happens outside the handler, so the error keeps no reference
    to the unmasked one.
    """
    try:
        return await awaitable
    except (ChezmoiFailed, GitFailed) as e:
        error = type(e)(e.argv, e.returncode, mask_diagnostic(masker, e.stderr, whole=whole))
    raise error


class WriteOps:
    """The one write path (DEC-8): pre-flight, prepare, execute.

    One instance per front end: its lock serializes cheztui's own writes and
    previews (DEC-27). It doesn't lock out another process.
    """

    def __init__(
        self, chezmoi: Chezmoi, config: Config, *, git_runner: Runner | None = None
    ) -> None:
        self.chezmoi = chezmoi
        self.config = config
        self._git_runner = git_runner if git_runner is not None else run_subprocess
        self._lock = asyncio.Lock()

    def git(self, preflight: Preflight) -> Git:
        """Git in chezmoi's effective working tree (spec §10, DEC-23)."""
        return Git(self._git_runner, work_tree=preflight.working_tree)

    def masker(self, preflight: Preflight) -> Masker:
        # `~` in the globs is chezmoi's destination (DEC-26).
        return Masker(self.config.secrets, preflight.dest_dir)

    async def preflight(self) -> Preflight:
        """Read effective config and porcelain status (spec §5 Pre-flight; DEC-8, DEC-23).

        Failures raise, their output masked whole since no target is known
        yet (DEC-30); they never read as a clean tree.
        """
        config = await sanitized(self.chezmoi.dump_config(), None, whole=True)
        git = Git(self._git_runner, work_tree=config.working_tree)
        stray = await sanitized(git.porcelain(), None, whole=True)
        return Preflight(
            source_dir=config.source_dir,
            dest_dir=config.dest_dir,
            working_tree=config.working_tree,
            auto_commit=config.auto_commit,
            stray=tuple(stray),
        )
