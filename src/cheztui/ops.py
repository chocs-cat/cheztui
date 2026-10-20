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
import contextlib
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, TypeVar

from cheztui.chezmoi import (
    Chezmoi,
    ChezmoiError,
    ChezmoiFailed,
    DumpEntry,
    EntryState,
    ManagedEntry,
    StatusRow,
)
from cheztui.config import Config, SecretsConfig
from cheztui.edit import Spec, SpecError, apply_spec, sha256, split_lines, unified_diff
from cheztui.git import Commit, Git, GitError, GitFailed
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


# --- The header's read (spec §4 Frame details, DEC-33) -----------------------


@dataclass(frozen=True)
class RepoState:
    source_dir: Path
    branch: str | None  # None when HEAD is detached
    ahead_behind: tuple[int, int] | None  # None with no upstream or before the first commit
    uncommitted: int  # porcelain entries in the working tree


async def repo_state(chezmoi: Chezmoi, *, git_runner: Runner = run_subprocess) -> RepoState:
    """Read the header's source, branch, ahead/behind and cleanliness (spec §4).

    `dump-config` first, then git in its `workingTree`, the three git reads
    at once. Errors propagate unmasked, as `drift()`'s do; front ends mask them.
    """
    config = await chezmoi.dump_config()
    git = Git(git_runner, work_tree=config.working_tree)
    branch, ahead_behind, stray = await asyncio.gather(
        git.branch(), git.ahead_behind(), git.porcelain()
    )
    return RepoState(config.source_dir, branch, ahead_behind, len(stray))


# --- The watch set (spec §5 Watching in the TUI, DEC-37) ---------------------


async def watch_targets(chezmoi: Chezmoi) -> tuple[Path, ...]:
    """The managed targets a watcher watches, sorted: every one but the scripts,
    which have no target (spec §5 *What is watched*).

    Read on its own, since the Drift read reads `managed` only when something
    drifted. Errors propagate unmasked, as `drift()`'s do.
    """
    managed = await chezmoi.managed(exclude=["scripts"])
    return tuple(sorted(entry.absolute for entry in managed.values()))


# --- The Drift detail's reads (spec §6 Rows and detail in the TUI; DEC-34) ---


async def masking_reasons(
    chezmoi: Chezmoi, masker: Masker, target: Path, source: Path | None, tags: Sequence[str]
) -> tuple[str, ...]:
    """A target's whole-masking reasons: `encrypted`, `always_mask`, `secret_template`.

    The one classification the write preview and the Drift detail share
    (spec §5 Secrets, Drift command plans; DEC-10, DEC-17, DEC-26, DEC-34).
    """
    reasons = []
    if "enc" in tags:
        reasons.append("encrypted")
    if masker.masks_whole(target):
        reasons.append("always_mask")
    if "tmpl" in tags and source is not None:
        # The literal source, transient: rendering can't map a line to a call.
        text = await sanitized(chezmoi.source_text(source), None, whole=True)
        if calls_secret_function(text):
            reasons.append("secret_template")
    return tuple(reasons)


_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _kind(line: str) -> str:
    """A hunk line's kind: `+`, `-`, `\\` (no newline at end of file), else context."""
    return line[0] if line[:1] in ("+", "-", "\\") else " "


def _hunk_range(start: int, count: int) -> str:
    # A count of 1 is left out, as chezmoi's diff prints it.
    return str(start) if count == 1 else f"{start},{count}"


def _cut_hunk(header: str, body: list[str], context: int) -> list[str]:
    """One hunk cut to `context` lines around its changes (spec §6 Drift).

    `header` is its `@@` line and `body` its lines, each with its ending.
    """
    match = _HUNK_HEADER.match(header)
    assert match is not None
    suffix = header[match.end() :]  # anything after the second `@@`, and the ending
    # The next old and new line numbers; a zero count's start is the line before.
    old = int(match[1]) + (match[2] == "0")
    new = int(match[3]) + (match[4] == "0")
    # Each line with the old and new line numbers it starts at, and the lines it
    # carries: a `\\ No newline at end of file` line goes with the line before.
    lines: list[tuple[str, int, int, list[str]]] = []
    for line in body:
        kind = _kind(line)
        if kind == "\\" and lines:
            lines[-1][3].append(line)
            continue
        lines.append((kind, old, new, [line]))
        old += kind in " -"
        new += kind in " +"
    changes = [i for i, (kind, *_) in enumerate(lines) if kind in "+-"]
    if not changes:
        return [header, *body]
    groups = [[changes[0], changes[0]]]
    for i in changes[1:]:
        if i - groups[-1][1] - 1 > 2 * context:
            groups.append([i, i])
        else:
            groups[-1][1] = i
    out: list[str] = []
    for first, last in groups:
        piece = lines[max(first - context, 0) : last + context + 1]
        old_at, new_at = piece[0][1], piece[0][2]
        old_count = sum(kind in " -" for kind, *_ in piece)
        new_count = sum(kind in " +" for kind, *_ in piece)
        # A zero count's start is the line before, as in any unified diff.
        old_range = _hunk_range(old_at if old_count else old_at - 1, old_count)
        new_range = _hunk_range(new_at if new_count else new_at - 1, new_count)
        out.append(f"@@ -{old_range} +{new_range} @@{suffix}")
        out += [text for *_, texts in piece for text in texts]
    return out


def cut_context(diff: str, context: int) -> str:
    """Cut a unified diff to `context` lines of context (spec §6 Drift, `ui.diff_context`).

    chezmoi's built-in diff has three; with fewer, each hunk keeps at most
    `context` before its first change and after its last, and a longer run
    between two changes splits it, each piece with its `@@` line recomputed.
    Lines outside hunks pass through; with `context` of 3 or more the diff
    comes back unchanged (DEC-34).
    """
    if context >= 3:
        return diff
    lines = split_lines(diff)
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _HUNK_HEADER.match(lines[i])
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        header = lines[i]
        # The hunk's extent comes from its counts, as masking reads it (spec §5
        # Secrets): a missing count is 1, an empty line is empty context.
        old = int(match[2]) if match[2] is not None else 1
        new = int(match[4]) if match[4] is not None else 1
        body: list[str] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            kind = _kind(line)
            if kind != "\\":
                if old <= 0 and new <= 0:
                    break
                if line[:1] not in (" ", "+", "-") and line.rstrip("\r\n"):
                    break  # not a hunk line: the diff is cut short
                old -= kind in " -"
                new -= kind in " +"
            body.append(line)
            i += 1
        out += _cut_hunk(header, body, context)
    return "".join(out)


@dataclass(frozen=True)
class DriftBody:
    """A Drift row's detail body as the pane shows it (spec §6 Rows and detail in the TUI)."""

    text: str
    masked: bool  # something in it was masked


async def drift_body(
    chezmoi: Chezmoi,
    secrets: SecretsConfig,
    row: DriftRow,
    dest_dir: Path,
    *,
    context: int,
    rendered: bool = False,
    reveal: bool = False,
) -> DriftBody:
    """The row's diff, masked as a diff and then cut, or with `rendered` its
    rendered output, masked as a body and not cut (spec §6; DEC-34).

    Masking uses the write preview's classification, with the globs against
    `dest_dir` (DEC-26); `reveal` skips it. Errors propagate unmasked, as
    `drift()`'s do; front ends mask them.
    """
    if rendered:
        text = _decode(await chezmoi.cat(row.target))
    else:
        text = await chezmoi.diff(row.target)
    if reveal:
        return DriftBody(text if rendered else cut_context(text, context), False)
    masker = Masker(secrets, dest_dir)
    reasons = await masking_reasons(chezmoi, masker, row.target, row.source, row.tags)
    done = masker.mask(text, row.target, diff=not rendered, whole=bool(reasons))
    if rendered:
        return DriftBody(done.text, done.masked)
    # Masked is decided on what is shown: a value masked on a context line the
    # cut drops leaves nothing to reveal (#40). The unmasked cut is transient.
    shown = cut_context(done.text, context)
    return DriftBody(shown, done.masked and shown != cut_context(text, context))


# --- The write path (spec §5 Write flow; DEC-8, DEC-27 to DEC-30) ------------

Action = Literal["apply", "re-add", "forget", "edit", "merge", "add"]

# The arguments after the global flags and before the targets (spec §5 Drift
# command plans, DEC-16, DEC-28).
ACTION_ARGS: dict[Action, tuple[str, ...]] = {
    "apply": ("apply", "--force", "--recursive=false"),
    "re-add": ("re-add", "--recursive=false"),
    "forget": ("forget", "--force"),
    "edit": ("edit", "--apply"),
    "merge": ("merge",),
    "add": ("add",),
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
    edit_command: str = ""

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
    env: tuple[tuple[str, str], ...] = ()

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
        """Apply-all always does, even with nothing ticked, so its rows can be
        ticked; otherwise interactive-only plans open no modal (spec §6 The
        preview modal, DEC-4)."""
        return self.apply_all or any(not c.interactive for c in self.commands)

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


# --- Result lines (spec §6 Drift *Result*, §8 Output; DEC-35) ----------------

VERBS: dict[Action, str] = {
    "apply": "applied",
    "re-add": "re-added",
    "forget": "forgot",
    "edit": "edited",
    "merge": "merged",
    "add": "added",
}


def targets_text(targets: Sequence[Path]) -> str:
    """The one target in `~` form, or `N targets`."""
    return display_target(targets[0]) if len(targets) == 1 else f"{len(targets)} targets"


def outcome_line(
    action: Action,
    targets: Sequence[Path],
    *,
    dry_run: bool,
    commit: Commit | None,
    pushed: bool | None,
    state: CommitState | None,
) -> str:
    """A command that ran, or would have: committed, no commit, or dry run.

    The lines `cheztui edit` and `add` print and the TUI's log line shows,
    from this one function, so the two can't word a commit differently (DEC-35).
    """
    name = targets_text(targets)
    if dry_run:
        return f"would {action} {name} (dry run)"
    if commit is not None:
        push = "pushed" if pushed else "not pushed"
        return f'{VERBS[action]} {name} → {commit.sha[:7]} "{commit.subject}" ({push})'
    return f"{VERBS[action]} {name} ({state})"


def failed(result: CommandResult) -> bool:
    return result.outcome in ("failed", "refused") or result.reporting_error is not None


def command_line(result: CommandResult) -> str:
    """One command result's line (spec §6 *Result*'s table)."""
    command = result.command
    name = targets_text(command.targets)
    if result.outcome == "failed":
        return f"✗ {command.action} {name} failed: {result.error}"
    if result.outcome == "refused":
        return f"✗ {command.action} {name} refused: {result.error}"
    if result.reporting_error is not None:
        return (
            f"✗ {command.action} {name} ran, but reading the result failed: "
            f"{result.reporting_error}"
        )
    return outcome_line(
        command.action,
        command.targets,
        dry_run=result.outcome == "dry run",
        commit=result.commit,
        pushed=result.pushed,
        state=result.state,
    )


@dataclass(frozen=True)
class ResultReport:
    """A `WriteResult` as the TUI shows it (spec §6 *Result*)."""

    line: str  # the log line; each failed part carries its own `✗`
    failed: bool  # the log line takes the error colour
    entries: tuple[tuple[str, str | None], ...]  # session-log entries: text and diagnostic


def report(result: WriteResult) -> ResultReport:
    """One entry per command result in order, a failed one with its whole masked
    stderr, a refused one with its stray paths; then `not run:` per unrun command.
    The log line joins the results' lines, or is `✗ <refused>` when nothing ran."""
    entries: list[tuple[str, str | None]] = []
    for r in result.results:
        diagnostic = None
        if r.outcome == "failed":
            diagnostic = r.stderr.rstrip("\n") or r.error  # DEC-36
        elif r.outcome == "refused" and r.stray:
            diagnostic = "\n".join(r.stray)
        entries.append((command_line(r), diagnostic))
    if not result.results:
        entries.append((f"✗ {result.refused}", None))
    entries += [(f"not run: {c.action} {targets_text(c.targets)}", None) for c in result.unrun]
    if not result.results:
        return ResultReport(f"✗ {result.refused}", True, tuple(entries))
    line = "; ".join(command_line(r) for r in result.results)
    return ResultReport(line, any(failed(r) for r in result.results), tuple(entries))


def left_out_line(entry: WriteEntry) -> str:
    """An excluded entry of an interactive plan, for the session log (spec §6)."""
    return f"left out of {entry.action}: {display_target(entry.target)} ({entry.excluded})"


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


def failure_message(error: ChezmoiError | GitError) -> str:
    """A failed read's one-line message (spec §4 Log line, §7).

    The CLI prints it after `cheztui: `, the TUI's log line after `✗ `. A
    failed command's message ends with its last stderr line.
    """
    if isinstance(error, ChezmoiFailed):
        tool, last = "chezmoi", error.last_line
    elif isinstance(error, GitFailed):
        lines = [line for line in error.stderr.splitlines() if line.strip()]
        tool, last = "git", lines[-1].strip() if lines else ""
    else:
        return str(error)
    return f"{tool} exited {error.returncode}" + (f": {last}" if last else "")


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


@dataclass(frozen=True)
class Refusal:
    message: str
    stray: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptResult:
    """Scripted edit/add output, with masked diff and diagnostics (spec §8)."""

    target: Path
    source: Path | None
    template: bool
    encrypted: bool
    diff: str = ""
    skipped: bool = False
    skip_text: str | None = None
    dry_run: bool = False
    commit: Commit | None = None
    pushed: bool | None = None
    verified: bool | None = None
    state: CommitState | None = None
    warning: tuple[str, ...] = ()
    stderr: str = ""
    error: str | None = None


def normalize_target(target: str | Path) -> Path:
    """Absolute with ~ expanded, without following symlinks (spec §8)."""
    return Path(os.path.abspath(Path(target).expanduser()))  # noqa: PTH100 (keep symlinks)


def display_target(target: Path) -> str:
    home = Path.home()
    if target.is_relative_to(home):
        return "~" if target == home else f"~/{target.relative_to(home)}"
    return str(target)


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
            edit_command=config.edit_command,
        )

    # --- Scripted edit (spec §8; DEC-11, DEC-18, DEC-31) -------------------

    async def edit(
        self,
        target: str | Path,
        spec: Spec,
        *,
        dry_run: bool = False,
        allow_drift: bool = False,
        reveal: bool = False,
    ) -> ScriptResult | Refusal:
        target = normalize_target(target)
        name = display_target(target)
        async with self._lock:
            managed = await self._managed_at(target)
            entry = next((e for e in managed.values() if e.absolute == target), None)
            if entry is None:
                return Refusal(f"{name} isn't managed by chezmoi; use cheztui add to add it")
            files, templates, encrypted, externals = await asyncio.gather(
                *(
                    sanitized(self.chezmoi.managed([kind], [target]), None, whole=True)
                    for kind in ("files", "templates", "encrypted", "externals")
                )
            )
            if any(e.absolute == target for e in externals.values()):
                return Refusal(f"{name} is an external; edit its .chezmoiexternal entry instead")
            if not any(e.absolute == target for e in files.values()):
                return Refusal(f"{name} isn't a regular file; cheztui edit edits one file's text")
            is_template = any(e.absolute == target for e in templates.values())
            is_encrypted = any(e.absolute == target for e in encrypted.values())
            status = await sanitized(self.chezmoi.status([target]), None, whole=True)
            if status and not allow_drift:
                return Refusal(
                    f"{name} has drifted ({status[0].columns}); applying would overwrite it. "
                    "Resolve it first, or pass --allow-drift"
                )
            pf = await self._script_preflight()
            if isinstance(pf, Refusal):
                return pf
            if pf.edit_command:
                return Refusal(
                    f"chezmoi's edit.command is set ({pf.edit_command}), so chezmoi won't run "
                    "cheztui's editor; unset it to use cheztui edit"
                )
            if pf.blocked:
                return Refusal("the source tree has uncommitted changes", pf.stray)
            source = entry.source_absolute
            if source is None:
                return Refusal(f"{name} has no source file")
            raw = await self._edit_bytes(source, is_encrypted)
            try:
                original = raw.decode("utf-8")
            except UnicodeError:
                return Refusal(f"{name} isn't UTF-8 text")
            try:
                new = apply_spec(spec, original, name)
            except SpecError as e:
                return Refusal(str(e))
            result = ScriptResult(
                target,
                source,
                is_template,
                is_encrypted,
                skipped=new is None,
                skip_text=spec.skip_if_contains,
                dry_run=dry_run or self.chezmoi.dry_run,
                warning=pf.stray if pf.warns else (),
            )
            if new is None:
                return result
            masker = self.masker(pf)
            diff = unified_diff(original, new, str(target.relative_to(pf.dest_dir)))
            whole = is_encrypted or masker.masks_whole(target)
            diff = (
                masker.mask(diff, target, diff=True, whole=True).text
                if whole and not reveal
                else diff
            )
            result = replace(result, diff=diff)
            if result.dry_run:
                return result
            tags = tuple(t for t, yes in (("tmpl", is_template), ("enc", is_encrypted)) if yes)
            write_entry = await self._masking(
                WriteEntry(target, source, "edit", True), tags, masker
            )
            prefix = tuple(self.chezmoi.argv())
            expected = sha256(new)
        # execute takes the same lock and rechecks pre-flight (DEC-27).
        fd, temp_name = tempfile.mkstemp(prefix="cheztui-edit-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"spec": spec.raw, "sha256": expected}, f)
            # -P keeps the working directory off sys.path (§8 step 7, DEC-18).
            editor = shlex.join([sys.executable, "-P", "-m", "cheztui", "_editor"])
            args = (
                "edit",
                "--apply",
                "--hardlink=false",
                *(("--force",) if allow_drift else ()),
                str(target),
            )
            command = WriteCommand(
                "edit",
                (target,),
                (*prefix, *args),
                False,
                (("EDITOR", editor), ("VISUAL", editor), ("CHEZTUI_EDIT_SPEC", temp_name)),
            )
            plan = WritePlan(pf, (write_entry,), (command,), prefix=prefix)
            outcome = await self.execute(plan, confirmed=True)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        result_or_refusal = self._script_result(result, outcome)
        if isinstance(result_or_refusal, Refusal) or result_or_refusal.error is not None:
            return result_or_refusal
        actual, verified = await asyncio.gather(
            self._edit_bytes(source, is_encrypted), self.chezmoi.verify(target)
        )
        verified = verified and hashlib.sha256(actual).hexdigest() == expected
        return replace(
            result_or_refusal,
            verified=verified,
            error=None if verified else f"{name} doesn't verify after the edit",
        )

    async def _managed_at(self, target: Path) -> dict[str, ManagedEntry]:
        """chezmoi's managed entries at `target`, none when it is outside the destination.

        managed rejects an outside-destination argument; only that rejection
        reads as not managed, decided by the effective destination. Inside it,
        the failure propagates (§8 step 1, DEC-32).
        """
        try:
            return await sanitized(self.chezmoi.managed(targets=[target]), None, whole=True)
        except ChezmoiFailed:
            cfg = await sanitized(self.chezmoi.dump_config(), None, whole=True)
            if target.is_relative_to(cfg.dest_dir):
                raise
            return {}

    async def _script_preflight(self) -> Preflight | Refusal:
        try:
            return await self.preflight()
        except GitError as e:
            return Refusal(f"pre-flight failed: {e}")

    async def _edit_bytes(self, source: Path, encrypted: bool) -> bytes:
        return await sanitized(
            self.chezmoi.decrypt(source) if encrypted else self.chezmoi.source_bytes(source),
            None,
            whole=True,
        )

    def _script_result(self, result: ScriptResult, outcome: WriteResult) -> ScriptResult | Refusal:
        if outcome.refused:
            return Refusal(outcome.refused)
        (done,) = outcome.results
        if done.outcome == "refused":
            return Refusal(done.error or "write refused", done.stray)
        error = done.error
        if done.reporting_error is not None:
            error = f"the command ran but reporting failed: {done.reporting_error}"
        return replace(
            result,
            commit=done.commit,
            pushed=done.pushed,
            state=done.state,
            warning=outcome.warning or result.warning,
            stderr=done.stderr,
            error=error,
            verified=False if error is not None else None,
        )

    # --- Add new files (spec §8 New files; DEC-19, DEC-31) ----------------

    async def add(
        self,
        target: str | Path,
        *,
        template: bool = False,
        encrypt: bool = False,
        dry_run: bool = False,
        reveal: bool = False,
    ) -> ScriptResult | Refusal:
        target = normalize_target(target)
        name = display_target(target)
        async with self._lock:
            # Outside the destination, the add dry run gives the refusal (§8, DEC-32).
            managed = await self._managed_at(target)
            if managed:
                if any(
                    e.absolute != target and e.absolute.is_relative_to(target)
                    for e in managed.values()
                ):
                    return Refusal(f"{name} contains managed entries; use cheztui edit on them")
                return Refusal(f"{name} is already managed; use cheztui edit")
            pf = await self._script_preflight()
            if isinstance(pf, Refusal):
                return pf
            if pf.blocked:
                return Refusal("the source tree has uncommitted changes", pf.stray)
            masker = self.masker(pf)
            whole = encrypt or masker.masks_whole(target)
            if target.is_dir() and not target.is_symlink():
                for root, _, files in os.walk(target, followlinks=False):
                    for filename in files:
                        path = Path(root) / filename
                        if not path.is_symlink() and path.is_file() and masker.masks_whole(path):
                            whole = True
            flags = (*(("--template",) if template else ()), *(("--encrypt",) if encrypt else ()))
            done = await self.chezmoi.run("add", *PREVIEW_FLAGS, *flags, str(target), check=False)
            if done.returncode or not done.stdout:
                stderr = _decode(done.stderr)
                error = (
                    short_error(stderr, "add", done.returncode)
                    if done.returncode
                    else (
                        "chezmoi would add nothing to the source"
                        + (f": {short_error(stderr, 'add', 0)}" if stderr.strip() else "")
                    )
                )
                return Refusal(mask_diagnostic(masker, error, whole=whole))
            diff = _decode(done.stdout)
            if whole and not reveal:
                diff = masker.mask(diff, target, diff=True, whole=True).text
            result = ScriptResult(
                target,
                None,
                template,
                encrypt,
                diff=diff,
                dry_run=dry_run or self.chezmoi.dry_run,
                warning=pf.stray if pf.warns else (),
            )
            if result.dry_run:
                return result
            prefix = tuple(self.chezmoi.argv())
            command = WriteCommand("add", (target,), (*prefix, "add", *flags, str(target)), False)
            # Directory diagnostics cover every added file, so inherit whole masking (§5).
            entry = WriteEntry(
                target,
                None,
                "add",
                True,
                masking=("encrypted" if encrypt else "always_mask",) if whole else (),
            )
            plan = WritePlan(pf, (entry,), (command,), prefix=prefix)
        outcome = await self.execute(plan, confirmed=True)
        result_or_refusal = self._script_result(result, outcome)
        if isinstance(result_or_refusal, Refusal) or result_or_refusal.error is not None:
            return result_or_refusal
        managed = await sanitized(self.chezmoi.managed(targets=[target]), None, whole=True)
        entry_after = next((e for e in managed.values() if e.absolute == target), None)
        if entry_after is None:
            return replace(result_or_refusal, verified=False, error="chezmoi added nothing")
        verified = await self.chezmoi.verify(target)
        return replace(
            result_or_refusal,
            source=entry_after.source_absolute,
            verified=verified,
            error=None if verified else f"{name} doesn't verify after the add",
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
        for e in entries:
            under = parent(e.target)
            if under is not None and e.target not in listed:
                implied[under].append(replace(e, selected=True, excluded=None, implied_by=under))
        result: list[WriteEntry] = []
        for e in entries:
            if parent(e.target) is None:
                result.append(e)
                result += sorted(implied.get(e.target, ()), key=lambda i: str(i.target))
        return result

    async def _masking(
        self, entry: WriteEntry, tags: tuple[str, ...], masker: Masker
    ) -> WriteEntry:
        """The entry's whole-masking reasons, as the Drift detail's (`masking_reasons`)."""
        if entry.excluded is not None:
            return entry
        reasons = await masking_reasons(self.chezmoi, masker, entry.target, entry.source, tags)
        return replace(entry, masking=reasons)

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

    # --- Execution (spec §5 Preparation and execution, Diagnostics; DEC-27, DEC-30)

    async def execute(
        self,
        plan: WritePlan,
        *,
        confirmed: bool,
        interactive_runner: Runner | None = None,
        interactive_env: Mapping[str, str] | None = None,
    ) -> WriteResult:
        """Run a prepared plan's commands in order, once each.

        A blocked, failed, unconfirmed or empty plan runs nothing; an
        interactive-only plan needs no confirmation (DEC-4). Each command
        gets a fresh pre-flight first. The first failure or refusal stops
        the rest, keeping earlier results; there is no rollback (DEC-27).
        Cancelling the caller neither kills a started write nor skips its
        result collection.

        Interactive commands (edit, merge) run through `interactive_runner`,
        the front end's terminal runner, with `interactive_env` merged over
        the environment; `chezmoi.py` still builds the argv (spec §10). In
        dry-run mode they never start: chezmoi's `edit --apply --dry-run`
        still edits the source (DEC-29).
        """
        if plan.prefix != tuple(self.chezmoi.argv()):
            raise ValueError("the plan was prepared with other chezmoi flags")
        dry_run = self.chezmoi.dry_run
        refusal = _refusal(plan, confirmed=confirmed)
        if refusal is not None:
            return WriteResult(unrun=plan.commands, refused=refusal, dry_run=dry_run)
        interactive = any(c.interactive for c in plan.commands)
        if interactive and interactive_runner is None and not dry_run:
            raise ValueError("an interactive command needs a runner")
        task = asyncio.ensure_future(self._execute(plan, interactive_runner, interactive_env))
        return await _uncancellable(task)

    async def _execute(
        self,
        plan: WritePlan,
        interactive_runner: Runner | None,
        interactive_env: Mapping[str, str] | None,
    ) -> WriteResult:
        async with self._lock:
            results: list[CommandResult] = []
            warning: tuple[str, ...] = ()
            commands = plan.commands
            for i, command in enumerate(commands):
                pf = await self._recheck(plan, command)
                if isinstance(pf, CommandResult):
                    results.append(pf)
                    return _result(results, commands[i + 1 :], warning, self.chezmoi.dry_run)
                if pf.warns:
                    warning = pf.stray
                if command.interactive and self.chezmoi.dry_run:
                    # The command that would run, after pre-flight; no commit (DEC-29).
                    results.append(CommandResult(command, "dry run", state="dry run"))
                    continue
                runner = interactive_runner if command.interactive else None
                env = dict(command.env)
                if command.interactive and interactive_env is not None:
                    env.update(interactive_env)
                result = await self._run(plan, command, pf, runner, env or None)
                results.append(result)
                if result.outcome == "failed" or result.reporting_error is not None:
                    return _result(results, commands[i + 1 :], warning, self.chezmoi.dry_run)
            return _result(results, (), warning, self.chezmoi.dry_run)

    async def _recheck(self, plan: WritePlan, command: WriteCommand) -> Preflight | CommandResult:
        """A fresh pre-flight before a command (DEC-27), or the refusal that stops here."""
        try:
            pf = await self.preflight()
        except (ChezmoiError, GitError) as e:  # already masked
            return _refused(command, f"pre-flight failed: {e}")
        if not pf.same_paths(plan.preflight):
            return _refused(command, "chezmoi's source, destination or working tree changed")
        if pf.blocked:
            return _refused(command, "the source tree has uncommitted changes", pf.stray)
        return pf

    async def _run(
        self,
        plan: WritePlan,
        command: WriteCommand,
        pf: Preflight,
        runner: Runner | None,
        env: Mapping[str, str] | None,
    ) -> CommandResult:
        """Run one command between two HEAD snapshots (spec §5; DEC-19, DEC-27)."""
        git = self.git(pf)
        masker = self.masker(pf)
        whole = _masks_whole(plan, command)

        def mask(text: str) -> str:
            return mask_diagnostic(masker, text, whole=whole)

        try:
            before = await git.head()
        except GitError as e:
            return _refused(command, f"pre-flight failed: {mask(str(e))}")
        args = command.argv[len(plan.prefix) :]
        try:
            done = await self.chezmoi.run(*args, check=False, runner=runner, env=env)
        except ChezmoiError as e:
            result = CommandResult(command, "failed", error=mask(str(e)))
        else:
            stderr = _decode(done.stderr)
            failed = done.returncode != 0
            error = short_error(stderr, command.action, done.returncode)
            if command.action == "edit" and not command.interactive:
                editor_errors = [
                    s.strip()
                    for s in stderr.splitlines()
                    if s.strip().startswith("cheztui _editor:")
                ]
                if editor_errors:
                    error = editor_errors[-1]
            result = CommandResult(
                command,
                "failed" if failed else "dry run" if self.chezmoi.dry_run else "ok",
                returncode=done.returncode,
                stdout=mask(_decode(done.stdout)),
                stderr=mask(stderr),
                error=mask(error) if failed else None,
            )
            del done, stderr  # raw output stays transient (DEC-30)
        return await self._collect(result, git, before, mask)

    async def _collect(
        self,
        result: CommandResult,
        git: Git,
        before: Commit | None,
        mask: Callable[[str], str],
    ) -> CommandResult:
        """HEAD after the command, push state and stray files, never fetching (DEC-19)."""
        try:
            after = await git.head()
            stray = tuple(await git.porcelain())
            changed = after is not None and (before is None or after.sha != before.sha)
            commit = after if changed else None
            pushed = await git.upstream_contains(commit.sha) if commit is not None else None
        except GitError as e:
            # The command ran; only reading its result failed (spec §5).
            return replace(result, reporting_error=mask(str(e)))
        state: CommitState = (
            "committed"
            if commit is not None
            else "dry run"
            if self.chezmoi.dry_run
            else "not committed"
            if stray
            else "no commit"
        )
        return replace(result, commit=commit, pushed=pushed, state=state, stray=stray)


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


def _refusal(plan: WritePlan, *, confirmed: bool) -> str | None:
    """Why a plan can't start, or None (spec §5, DEC-27)."""
    if plan.preflight.blocked:
        return "the source tree has uncommitted changes"
    if plan.error is not None:
        return "the preview failed"
    if not plan.commands:
        return "nothing is selected"
    if plan.needs_confirmation and not confirmed:
        return "not confirmed"
    return None


def _refused(command: WriteCommand, reason: str, stray: tuple[str, ...] = ()) -> CommandResult:
    """A command that didn't run: nothing it would write has changed."""
    return CommandResult(command, "refused", error=reason, stray=stray)


def _masks_whole(plan: WritePlan, command: WriteCommand) -> bool:
    """Grouped output is masked whole if any affected target needs it (DEC-30)."""
    targets = set(command.targets)
    return any(e.masking for e in plan.entries if e.target in targets or e.implied_by in targets)


def _result(
    results: Sequence[CommandResult],
    unrun: Sequence[WriteCommand],
    warning: tuple[str, ...],
    dry_run: bool,
) -> WriteResult:
    return WriteResult(
        results=tuple(results),
        unrun=tuple(unrun),
        warning=warning,
        dry_run=dry_run,
        # Any attempted command means views refresh, failure included (spec §5).
        refresh=any(r.outcome != "refused" for r in results),
    )


async def _uncancellable(task: asyncio.Future[_T]) -> _T:
    """Await a write; a cancelled caller still waits for it to finish (spec §5, DEC-27)."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        raise
