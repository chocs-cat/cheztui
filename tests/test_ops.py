"""The Drift model (spec §6 Drift; DEC-14, DEC-15, DEC-19).

`build_drift` is driven from the recorded fixtures and from hand-built
reads; `drift` runs against the real chezmoi in the scratch setup.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from cheztui.chezmoi import (
    Chezmoi,
    DumpEntry,
    EntryState,
    ManagedEntry,
    StatusRow,
    parse_dump,
    parse_managed,
    parse_state_dump,
    parse_status,
)
from cheztui.ops import Drift, DriftRow, build_drift, drift, needs_cat
from cheztui.runner import Completed, run_subprocess
from tests.conftest import Scratch, requires_chezmoi
from tests.test_chezmoi_reads import HOME, ROOT, fixture
from tests.test_chezmoi_reads import build_drift as build_drift_setup


def summary(d: Drift) -> list[tuple[str, str, str, str, tuple[str, ...]]]:
    return [(r.target.name, r.columns, r.direction, r.default_fix, r.tags) for r in d.rows]


# --- From the recorded fixtures -------------------------------------------------


def fixture_drift() -> Drift:
    return build_drift(
        parse_status(fixture("status.txt")),
        parse_managed(fixture("managed.json")),
        parse_managed(fixture("managed-templates.json")),
        {},
        {},
        parse_dump(fixture("dump.json")),
        parse_state_dump(fixture("state-dump.json")),
    )


def test_fixture_rows_in_sort_order():
    assert summary(fixture_drift()) == [
        (".both file", "MM", "both", "merge", ()),
        (".gone", "DA", "you", "apply", ()),
        (".mine", "MM", "you", "re-add", ()),
        (".tool", " M", "src", "apply", ("exec",)),
        ("hello.sh", " R", "run", "apply", ()),
    ]


def test_fixture_sources_and_kinds():
    rows = {r.target.name: r for r in fixture_drift().rows}
    assert rows[".both file"].source == ROOT / "source" / "dot_both file"
    assert rows[".tool"].source == ROOT / "source" / "executable_dot_tool"
    assert rows["hello.sh"].source == ROOT / "source" / "run_onchange_hello.sh"
    assert rows["hello.sh"].kind == "script"
    assert rows[".mine"].kind == "file"
    assert not any(r.fix_overwrites for r in rows.values())


def test_fixture_verify():
    d = fixture_drift()
    assert d.verified is False
    assert d.failing == tuple(
        HOME / n for n in (".both file", ".gone", ".mine", ".tool", "hello.sh")
    )


def test_empty_drift_is_verified():
    assert Drift().verified is True
    assert Drift().failing == ()


# --- Hand-built reads -----------------------------------------------------------

H = Path("/h")
S = Path("/s")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def managed(*names: str, source_relative: str | None = None) -> dict[str, ManagedEntry]:
    return {
        n: ManagedEntry(
            relative=n,
            absolute=H / n,
            source_absolute=S / (source_relative if source_relative is not None else n),
            source_relative=source_relative if source_relative is not None else n,
        )
        for n in names
    }


def file_entry(name: str, contents: str | None, perm: int = 0o644) -> DumpEntry:
    return DumpEntry(name, "file", perm, contents, None, None)


def build(
    status: Sequence[tuple[str, str]],
    dump: Mapping[str, DumpEntry],
    states: Mapping[str, EntryState],
    *,
    managed_entries: Mapping[str, ManagedEntry] | None = None,
    templates: Sequence[str] = (),
    encrypted: Sequence[str] = (),
    externals: Sequence[str] = (),
    raw: Mapping[Path, bytes] | None = None,
) -> Drift:
    rows = [StatusRow(c[0], c[1], H / n) for c, n in status]
    m = managed_entries if managed_entries is not None else managed(*(n for _, n in status))
    return build_drift(
        rows,
        m,
        {n: m[n] for n in templates},
        {n: m[n] for n in encrypted},
        {n: m[n] for n in externals},
        dump,
        {H / n: s for n, s in states.items()},
        raw,
    )


def only(d: Drift) -> DriftRow:
    assert len(d.rows) == 1
    return d.rows[0]


def dir_entry(perm: int) -> DumpEntry:
    return DumpEntry(".d", "dir", perm, None, None, None)


DIR_BIT = 0x80000000  # Go's os.ModeDir, which entryState's mode carries


@pytest.mark.parametrize(
    ("perm", "direction"), [(0o755, "you"), (0o700, "both")], ids=["equal", "different"]
)
def test_directory_compares_permission_bits(perm, direction):
    row = only(
        build(
            [("MM", ".d")],
            {".d": dir_entry(perm)},
            {".d": EntryState("dir", DIR_BIT | 0o755, None)},
        )
    )
    assert (row.direction, row.default_fix, row.fix_overwrites) == (direction, "apply", True)
    assert (row.kind, row.tags) == ("dir", ("dir",))


def test_a_type_change_is_both():
    row = only(
        build(
            [("MM", ".x")],
            {".x": file_entry(".x", "x\n")},
            {".x": EntryState("symlink", None, sha(b"x\n"))},
        )
    )
    assert (row.direction, row.default_fix, row.fix_overwrites) == ("both", "merge", False)


def test_no_entry_state_is_both():
    row = only(build([("MM", ".x")], {".x": file_entry(".x", "x\n")}, {}))
    assert (row.direction, row.default_fix) == ("both", "merge")


def test_a_symlink_hashes_its_linkname():
    link = DumpEntry(".l", "symlink", None, None, "target", None)
    you = only(
        build([("MM", ".l")], {".l": link}, {".l": EntryState("symlink", None, sha(b"target"))})
    )
    assert (you.direction, you.default_fix, you.fix_overwrites) == ("you", "apply", True)
    both = only(
        build([("MM", ".l")], {".l": link}, {".l": EntryState("symlink", None, sha(b"old"))})
    )
    assert (both.direction, both.default_fix, both.fix_overwrites) == ("both", "apply", True)
    assert both.tags == ("link",)


def test_no_target_state_entry_is_both_and_apply():
    row = only(build([("MM", ".x")], {}, {".x": EntryState("file", 0o644, sha(b"x\n"))}))
    assert (row.direction, row.default_fix, row.fix_overwrites, row.kind) == (
        "both",
        "apply",
        True,
        None,
    )


def test_a_target_not_in_managed_has_no_source_and_no_entry():
    row = only(build([("MM", ".x")], {".x": file_entry(".x", "x\n")}, {}, managed_entries={}))
    assert (row.source, row.kind, row.direction, row.default_fix) == (None, None, "both", "apply")


def test_empty_source_relative_has_no_source():
    m = managed(".x", source_relative="")
    row = only(
        build([("MD", ".x")], {}, {".x": EntryState("file", 0o644, sha(b"x"))}, managed_entries=m)
    )
    assert row.source is None
    assert (row.direction, row.default_fix, row.fix_overwrites) == ("both", "apply", True)


def test_source_path_from_managed():
    m = managed(".x", source_relative="dot_x")
    row = only(build([(" M", ".x")], {".x": file_entry(".x", "x")}, {}, managed_entries=m))
    assert row.source == S / "dot_x"


def test_an_empty_file_hashes_no_bytes():
    row = only(
        build(
            [("MM", ".e")],
            {".e": file_entry(".e", None)},
            {".e": EntryState("file", 0o644, sha(b""))},
        )
    )
    assert row.direction == "you"


def test_a_ufffd_row_hashes_the_raw_bytes():
    raw = b"\xff\xfe\x00bin"
    entry = file_entry(".bin", "��\x00bin")
    state = {".bin": EntryState("file", 0o644, sha(raw))}
    assert needs_cat(StatusRow("M", "M", H / ".bin"), entry)
    with_raw = only(build([("MM", ".bin")], {".bin": entry}, state, raw={H / ".bin": raw}))
    assert (with_raw.direction, with_raw.default_fix) == ("you", "re-add")
    # Without the bytes the dumped string can never match: the case #11 fixes.
    assert only(build([("MM", ".bin")], {".bin": entry}, state)).direction == "both"


@pytest.mark.parametrize(
    ("columns", "entry", "expected"),
    [
        ("MM", file_entry(".x", "plain"), False),
        (" M", file_entry(".x", "�"), False),
        (" R", file_entry(".x", "�"), False),
        ("MM", DumpEntry(".x", "symlink", None, None, "�", None), False),
        ("MM", None, False),
        ("MM", file_entry(".x", "�"), True),
    ],
)
def test_needs_cat(columns, entry, expected):
    assert needs_cat(StatusRow(columns[0], columns[1], H / ".x"), entry) is expected


def test_rows_with_a_blank_second_column_are_dropped():
    assert build([("M ", ".x")], {}, {}).rows == ()


def test_deleted_target_is_apply_whatever_its_direction():
    row = only(build([("DA", ".g")], {".g": file_entry(".g", "g")}, {}))
    assert (row.direction, row.default_fix, row.fix_overwrites) == ("both", "apply", False)


def test_you_on_a_template_is_merge():
    row = only(
        build(
            [("MM", ".t")],
            {".t": file_entry(".t", "T")},
            {".t": EntryState("file", 0o644, sha(b"T"))},
            templates=[".t"],
        )
    )
    assert (row.direction, row.default_fix, row.tags) == ("you", "merge", ("tmpl",))


def test_tags_in_order():
    row = only(
        build(
            [(" M", ".x")],
            {".x": file_entry(".x", "x", perm=0o700)},
            {},
            templates=[".x"],
            encrypted=[".x"],
            externals=[".x"],
        )
    )
    assert row.tags == ("tmpl", "enc", "exec", "ext")


def test_sort_by_direction_then_code_points():
    d = build(
        [(" R", "a.sh"), (" M", "B"), (" M", "a"), ("MM", "z"), ("MM", "Z")],
        {n: file_entry(n, n) for n in ("a.sh", "B", "a", "z", "Z")},
        {"z": EntryState("file", 0o644, sha(b"z"))},
    )
    assert [(r.direction, r.target.name) for r in d.rows] == [
        ("both", "Z"),
        ("you", "z"),
        ("src", "B"),
        ("src", "a"),
        ("run", "a.sh"),
    ]


# --- Live: the installed chezmoi ------------------------------------------------


class Recording:
    """A runner that records each chezmoi command and runs it for real."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str], **kwargs) -> Completed:
        self.calls.append(tuple(argv))
        return await run_subprocess(argv, **kwargs)


def recording(s: Scratch) -> tuple[Chezmoi, Recording]:
    r = Recording()
    return Chezmoi(r, config=s.config, source=s.source, destination=s.home), r


def rows_by_name(d: Drift) -> dict[str, DriftRow]:
    return {r.target.name: r for r in d.rows}


@requires_chezmoi
async def test_live_drift(scratch: Scratch):
    build_drift_setup(scratch)
    d = await drift(scratch.chezmoi())
    assert summary(d) == [
        (".both file", "MM", "both", "merge", ()),
        (".gone", "DA", "you", "apply", ()),
        (".mine", "MM", "you", "re-add", ()),
        (".tool", " M", "src", "apply", ("exec",)),
        ("hello.sh", " R", "run", "apply", ()),
    ]
    assert rows_by_name(d)[".mine"].source == scratch.source / "dot_mine"
    assert d.verified is False
    assert d.failing == tuple(r.target for r in d.rows)
    assert "hello.sh" in [t.name for t in d.failing]


@requires_chezmoi
async def test_live_binary_file(scratch: Scratch):
    # #11: bytes that aren't UTF-8 dump as U+FFFD; the hash comes from `chezmoi cat`.
    h = scratch.home
    (h / ".bin").write_bytes(b"\xff\xfe\x00bin")
    (h / ".bin2").write_bytes(b"\xff\xfe\x00two")
    scratch.run("add", str(h / ".bin"), str(h / ".bin2"))
    with (h / ".bin").open("ab") as f:
        f.write(b"\xffhome")
    with (h / ".bin2").open("ab") as f:
        f.write(b"\xffhome")
    with (scratch.source / "dot_bin2").open("ab") as f:
        f.write(b"\xffsource")
    cz, rec = recording(scratch)
    rows = rows_by_name(await drift(cz))
    assert (rows[".bin"].direction, rows[".bin"].default_fix) == ("you", "re-add")
    assert (rows[".bin2"].direction, rows[".bin2"].default_fix) == ("both", "merge")
    assert sum(1 for c in rec.calls if "cat" in c) == 2


@requires_chezmoi
async def test_live_template_dir_symlink_and_remove(scratch: Scratch):
    h, src = scratch.home, scratch.source
    (src / "dot_t.tmpl").write_text('{{ "t" | upper }}\n')
    (h / ".d").mkdir()
    (h / ".l").symlink_to("one")
    (h / ".rm").write_text("rm\n")
    scratch.run("add", str(h / ".d"), str(h / ".l"), str(h / ".rm"))
    scratch.run("apply", "--force")

    with (h / ".t").open("a") as f:
        f.write("edited\n")
    (h / ".d").chmod(0o700)
    (h / ".l").unlink()
    (h / ".l").symlink_to("two")
    (src / "symlink_dot_l").write_text("three\n")
    # A target moved to .chezmoiremove and then edited shows `MD`.
    (src / "dot_rm").unlink()
    (src / ".chezmoiremove").write_text(".rm\n")
    with (h / ".rm").open("a") as f:
        f.write("edited\n")

    rows = rows_by_name(await drift(scratch.chezmoi()))
    assert fix(rows[".t"]) == ("you", "merge", ("tmpl",), False)
    assert fix(rows[".d"]) == ("you", "apply", ("dir",), True)
    assert fix(rows[".l"]) == ("both", "apply", ("link",), True)
    rm = rows[".rm"]
    assert (rm.columns, rm.source, rm.kind) == ("MD", None, None)
    assert fix(rm) == ("both", "apply", (), True)


def fix(row: DriftRow) -> tuple[str, str, tuple[str, ...], bool]:
    return (row.direction, row.default_fix, row.tags, row.fix_overwrites)


@requires_chezmoi
async def test_live_pending_script_fails_verify(scratch: Scratch):
    (scratch.source / "run_once_setup.sh").write_text("#!/bin/sh\ntrue\n")
    d = await drift(scratch.chezmoi())
    assert summary(d) == [("setup.sh", " R", "run", "apply", ())]
    assert d.verified is False
    assert d.failing == (scratch.home / "setup.sh",)


@requires_chezmoi
async def test_live_clean_setup_reads_only_status(scratch: Scratch):
    (scratch.home / ".x").write_text("x\n")
    scratch.run("add", str(scratch.home / ".x"))
    cz, rec = recording(scratch)
    assert await drift(cz) == Drift()
    assert len(rec.calls) == 1
    assert "status" in rec.calls[0]
