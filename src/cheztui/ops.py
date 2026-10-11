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
import os
import shlex
import stat
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from cheztui.secrets import Masker, calls_secret_function

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


def _drift_order(row: DriftRow) -> tuple[int, str]:
    """The Drift view's sort (spec §6 Drift)."""
    return (_DIRECTION_ORDER[row.direction], str(row.target))


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
    rows.sort(key=_drift_order)
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

    @property
    def dry_run(self) -> bool:
        return "--dry-run" in self.prefix

    def with_selection(self, targets: Iterable[Path]) -> WritePlan:
        """The plan re-ticked to exactly `targets`, previews kept (spec §5, DEC-16).

        Excluded entries stay unticked, and a forgotten directory's
        descendants follow their directory's tick (DEC-28).
        """
        chosen = set(targets)
        top = {
            e.target
            for e in self.entries
            if e.implied_by is None and e.excluded is None and e.target in chosen
        }
        entries = tuple(
            replace(
                e, selected=e.implied_by in top if e.implied_by is not None else e.target in top
            )
            for e in self.entries
        )
        return replace(self, entries=entries, commands=_commands(entries, self.prefix))


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

    # --- Preparation (spec §5 Drift command plans; DEC-16, DEC-28) ---------

    async def prepare(self, action: Action, rows: Sequence[DriftRow]) -> WritePlan:
        """One action over the selected rows (or the current row), in Drift order.

        `edit` takes exactly one row; every other action is one command.
        """
        rows = _deduplicated(rows)
        if action == "edit" and len(rows) > 1:
            raise ValueError("edit takes one target")
        return await self._prepare([(action, rows)])

    async def prepare_defaults(self, rows: Sequence[DriftRow]) -> WritePlan:
        """Each row's default fix, grouped apply, re-add, merge (spec §5, DEC-28)."""
        rows = _deduplicated(rows)
        fixes: tuple[Fix, ...] = ("apply", "re-add", "merge")
        groups = [(fix, [r for r in rows if r.default_fix == fix]) for fix in fixes]
        return await self._prepare([(fix, group) for fix, group in groups if group])

    async def prepare_apply_all(self, rows: Sequence[DriftRow]) -> WritePlan:
        """Every row, ticking exactly `src→` and `run` (DEC-16; #15 is still open)."""
        rows = _deduplicated(rows)
        ticked = {r.target for r in rows if r.direction in ("src", "run")}
        return await self._prepare([("apply", rows)], ticked=ticked)

    async def _prepare(
        self,
        groups: Sequence[tuple[Action, Sequence[DriftRow]]],
        *,
        ticked: set[Path] | None = None,
    ) -> WritePlan:
        async with self._lock:
            # A fresh pre-flight before any writing dry run (DEC-8, DEC-27).
            pf = await self.preflight()
            tags = {r.target: r.tags for _, rows in groups for r in rows}
            entries: list[WriteEntry] = []
            for action, rows in groups:
                group = [_entry(action, row, ticked) for row in rows]
                if action == "forget":
                    group = await self._with_descendants(group, tags)
                entries += group
            prefix = tuple(self.chezmoi.argv())
            plan = WritePlan(
                pf,
                tuple(entries),
                _commands(entries, prefix),
                apply_all=ticked is not None,
                prefix=prefix,
            )
            if pf.blocked:
                return plan  # nothing that writes has run, dry runs included
            masker = self.masker(pf)
            masked = [await self._masking(e, tags.get(e.target, ()), masker) for e in entries]
            return await self._previews(replace(plan, entries=tuple(masked)), masker)

    async def _with_descendants(
        self, entries: list[WriteEntry], tags: dict[Path, tuple[str, ...]]
    ) -> list[WriteEntry]:
        """Add every managed descendant of a forgotten directory (spec §5, DEC-28).

        They are previewed but not named in the command; a selected row
        under a forgotten directory becomes one of them. Tags for added
        descendants come from chezmoi's metadata (DEC-15), into `tags`.
        """
        dirs = sorted(
            (e.target for e in entries if e.excluded is None and "dir" in tags.get(e.target, ())),
            key=lambda t: len(t.parts),
        )
        if not dirs:
            return entries

        def parent(target: Path) -> Path | None:
            return next((d for d in dirs if target != d and target.is_relative_to(d)), None)

        managed, templates, encrypted = await asyncio.gather(
            sanitized(self.chezmoi.managed(), None, whole=True),
            sanitized(self.chezmoi.managed(["templates"]), None, whole=True),
            sanitized(self.chezmoi.managed(["encrypted"]), None, whole=True),
        )
        rows = {e.target: e for e in entries}
        implied: dict[Path, list[WriteEntry]] = {d: [] for d in dirs}
        for m in sorted(managed.values(), key=lambda m: str(m.absolute)):
            under = parent(m.absolute)
            if under is None:
                continue
            row = rows.get(m.absolute)
            tags.setdefault(
                m.absolute,
                tuple(
                    t for t, inc in (("tmpl", templates), ("enc", encrypted)) if m.relative in inc
                ),
            )
            implied[under].append(
                WriteEntry(
                    target=m.absolute,
                    source=m.source_absolute if m.source_relative else None,
                    action="forget",
                    selected=True,
                    implied_by=under,
                    direction=row.direction if row is not None else None,
                )
            )
        listed = {e.target for group in implied.values() for e in group}
        result: list[WriteEntry] = []
        for e in entries:
            under = parent(e.target)
            if under is None:
                result.append(e)
                result += implied.get(e.target, ())
            elif e.target not in listed:
                implied[under].append(replace(e, selected=True, excluded=None, implied_by=under))
        return result

    async def _masking(
        self, entry: WriteEntry, tags: tuple[str, ...], masker: Masker
    ) -> WriteEntry:
        """The entry's whole-masking reasons (spec §5 Secrets; DEC-10, DEC-17, DEC-26)."""
        if entry.excluded is not None:
            return entry
        reasons = []
        if "enc" in tags:
            reasons.append("encrypted")
        if masker.masks_whole(entry.target):
            reasons.append("always_mask")
        if "tmpl" in tags and entry.source is not None:
            # The literal source, transient: rendering can't map a line to a call.
            source = await sanitized(self.chezmoi.source_text(entry.source), None, whole=True)
            if calls_secret_function(source):
                reasons.append("secret_template")
        return replace(entry, masking=tuple(reasons))

    async def _previews(self, plan: WritePlan, masker: Masker) -> WritePlan:
        """Dry-run each target on its own and mask it (spec §5 Drift command plans).

        The first failure blocks the plan with its masked error.
        """
        entries = list(plan.entries)
        for i, e in enumerate(entries):
            if e.excluded is not None or e.action in INTERACTIVE:
                continue
            args = (*ACTION_ARGS[e.action], *PREVIEW_FLAGS, str(e.target))
            done = await self.chezmoi.run(*args, check=False)
            whole, never_mask = _preview_rule(e, entries)
            rule = {"whole": whole, "never_mask": never_mask}
            if done.returncode != 0:
                stderr = _decode(done.stderr)
                error = short_error(stderr, e.action, done.returncode)
                return replace(
                    plan,
                    entries=tuple(entries),
                    error=masker.mask(error, e.target, **rule).text,
                    diagnostic=masker.mask(stderr, e.target, **rule).text,
                )
            diff = masker.mask(_decode(done.stdout), e.target, diff=True, **rule).text
            entries[i] = replace(e, diff=diff)
        return replace(plan, entries=tuple(entries))


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _deduplicated(rows: Sequence[DriftRow]) -> list[DriftRow]:
    """One row per target, in Drift order (spec §5)."""
    return sorted({r.target: r for r in reversed(rows)}.values(), key=_drift_order)


def eligibility(action: Action, row: DriftRow) -> str | None:
    """Why `action` can't take `row`, or None (spec §5 Drift command plans, §6; DEC-28).

    From chezmoi's metadata and the destination's file type, never from
    source names (DEC-13, DEC-15).
    """
    if action == "apply":
        return None
    if row.source is None:
        return "no source"
    if "ext" in row.tags:
        return "an external"
    if action == "forget":
        return None
    if row.kind is None:
        return "no target-state entry"
    if row.kind == "script":
        return "a script"
    if action == "edit":
        return None if row.kind in ("file", "symlink") else f"not a file or symlink ({row.kind})"
    if row.kind == "dir":
        return "a directory"
    if row.kind == "symlink":
        return "a symlink"
    if row.kind != "file":
        return f"not a regular file ({row.kind})"
    if row.columns[0] == "D":
        return "target deleted"
    try:
        mode = os.lstat(row.target).st_mode
    except FileNotFoundError:
        return "target deleted"
    if not stat.S_ISREG(mode):
        return "target isn't a regular file"
    if action == "re-add" and "tmpl" in row.tags:
        return "a template"
    return None


def _entry(action: Action, row: DriftRow, ticked: set[Path] | None) -> WriteEntry:
    excluded = eligibility(action, row)
    return WriteEntry(
        target=row.target,
        source=row.source,
        action=action,
        selected=excluded is None and (ticked is None or row.target in ticked),
        # Warned whatever the row's default fix (spec §5, DEC-16).
        overwrites=action == "apply" and row.direction in ("you", "both"),
        excluded=excluded,
        direction=row.direction,
    )


def _preview_rule(entry: WriteEntry, entries: Sequence[WriteEntry]) -> tuple[bool, bool]:
    """(whole, never_mask) for an entry's preview.

    A forgotten directory's diff covers its descendants: whole if any needs
    it, else line level without `never_mask` (spec §5).
    """
    group = [e for e in entries if e.implied_by == entry.target]
    if not group:
        return bool(entry.masking), True
    return any(e.masking for e in (entry, *group)), False


def _commands(entries: Iterable[WriteEntry], prefix: tuple[str, ...]) -> tuple[WriteCommand, ...]:
    """One command per action group, naming only its selected targets (DEC-28)."""
    groups: dict[Action, list[Path]] = {}
    for e in entries:
        targets = groups.setdefault(e.action, [])
        if e.selected and e.excluded is None and e.implied_by is None:
            targets.append(e.target)
    commands = []
    for action, targets in groups.items():
        # An empty group never becomes an unscoped chezmoi call.
        batches = [[t] for t in targets] if action == "edit" else [targets] if targets else []
        commands += [
            WriteCommand(
                action,
                tuple(batch),
                (*prefix, *ACTION_ARGS[action], *map(str, batch)),
                action in INTERACTIVE,
            )
            for batch in batches
        ]
    return tuple(commands)
