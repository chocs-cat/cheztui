"""cheztui's behaviour, shared by the CLI and the TUI (spec §7, §10).

The Drift model (spec §6 Drift; DEC-14, DEC-15): `drift()` reads chezmoi
and `build_drift()` turns the reads into rows. `build_drift` does no I/O,
so the unit tests and the TUI's fake runner share it.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cheztui.chezmoi import Chezmoi, DumpEntry, EntryState, ManagedEntry, StatusRow

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
