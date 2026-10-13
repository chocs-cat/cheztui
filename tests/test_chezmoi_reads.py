"""chezmoi's output parsers and the typed reads (spec §6 Drift, §10; DEC-13 to DEC-15).

The parsers run against fixtures recorded from real chezmoi (spec §11). The
live tests build the same setup and read it through the installed chezmoi, so
a change in its output format fails here. `test_record_fixtures` rewrites the
fixtures; tests/fixtures/chezmoi/README.md says how to run it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from cheztui.chezmoi import (
    ChezmoiUnavailable,
    parse_dump,
    parse_dump_config,
    parse_managed,
    parse_state_dump,
    parse_status,
)
from tests.conftest import Scratch, requires_chezmoi

FIXTURES = Path(__file__).parent / "fixtures" / "chezmoi"
ROOT = Path("/scratch")
HOME = ROOT / "home"
DIFF_ARGS = ("diff", "--no-pager", "--color=false", "--use-builtin-diff")  # Chezmoi.diff


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def build_drift(s: Scratch) -> None:
    """A source and home with one of each kind of row the Drift view tells apart.

    `.mine` you→ (MM) · `.both file` both (MM) · `.tool` src→ ( M), executable ·
    `.tmplfile` src→ ( M), a template · `hello.sh` an onchange script ( R) ·
    `.gone` deleted (DA) · `.dir` a directory · `.link` a symlink.
    """
    h = s.home
    (h / ".mine").write_text("mine\n")
    (h / ".both file").write_text("both\n")
    (h / ".tool").write_text("#!/bin/sh\necho tool\n")
    (h / ".tool").chmod(0o755)
    (h / ".gone").write_text("gone\n")
    (h / ".dir").mkdir()
    (h / ".dir" / "inner").write_text("inner\n")
    (h / ".link").symlink_to(".mine")
    s.run("add", *(str(h / n) for n in (".mine", ".both file", ".tool", ".gone", ".dir", ".link")))
    (s.source / "dot_tmplfile.tmpl").write_text('{{ "tmpl" | upper }}\n')
    # A script needs its #! line, or chezmoi's apply fails with "exec format error".
    (s.source / "run_onchange_hello.sh").write_text("#!/bin/sh\necho hello >/dev/null\n")
    s.run("apply", "--force")
    s.commit_all("initial")

    with (h / ".mine").open("a") as f:
        f.write("edited in home\n")
    with (h / ".both file").open("a") as f:
        f.write("edited in home\n")
    with (s.source / "dot_both file").open("a") as f:
        f.write("edited in source\n")
    with (s.source / "executable_dot_tool").open("a") as f:
        f.write("echo more\n")
    (s.source / "run_onchange_hello.sh").write_text("#!/bin/sh\necho changed >/dev/null\n")
    (s.source / "dot_tmplfile.tmpl").write_text('{{ "tmpl" | upper }}\n{{ "more" | upper }}\n')
    (h / ".gone").unlink()


# --- Status -------------------------------------------------------------------


def test_status_fixture():
    rows = parse_status(fixture("status.txt"))
    assert [(r.columns, r.target) for r in rows] == [
        ("MM", HOME / ".both file"),
        ("DA", HOME / ".gone"),
        ("MM", HOME / ".mine"),
        (" M", HOME / ".tmplfile"),
        (" M", HOME / ".tool"),
        (" R", HOME / "hello.sh"),
    ]
    assert (rows[1].first, rows[1].second) == ("D", "A")


def test_status_keeps_odd_names_exactly():
    rows = parse_status("MM /h/ trailing \n M /h/a\rb\x0bc\n\n")
    assert [r.target for r in rows] == [Path("/h/ trailing "), Path("/h/a\rb\x0bc")]


def test_status_empty():
    assert parse_status("") == []


@pytest.mark.parametrize("text", ["MM\n", "MMx/h/a\n", "M /h\n", "MM/h/a\n"])
def test_status_malformed(text):
    with pytest.raises(ChezmoiUnavailable, match="status"):
        parse_status(text)


# --- Managed ------------------------------------------------------------------


def test_managed_fixture():
    entries = parse_managed(fixture("managed.json"))
    assert set(entries) == {
        ".both file",
        ".dir",
        ".dir/inner",
        ".gone",
        ".link",
        ".mine",
        ".tmplfile",
        ".tool",
        "hello.sh",
    }
    both = entries[".both file"]
    assert both.relative == ".both file"
    assert both.absolute == HOME / ".both file"
    assert both.source_absolute == ROOT / "source" / "dot_both file"
    assert both.source_relative == "dot_both file"


def test_managed_templates_fixture():
    assert list(parse_managed(fixture("managed-templates.json"))) == [".tmplfile"]


def test_managed_without_source_paths():
    entries = parse_managed('{".x": {"absolute": "/h/.x"}}')
    assert entries[".x"].source_absolute is None
    assert entries[".x"].source_relative is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{".x": []}',
        '{".x": {}}',
        '{".x": {"absolute": 3}}',
        '{".x": {"absolute": "/h/.x", "sourceAbsolute": false}}',
    ],
)
def test_managed_malformed(text):
    with pytest.raises(ChezmoiUnavailable, match="managed"):
        parse_managed(text)


# --- Dump ---------------------------------------------------------------------


def test_dump_fixture():
    entries = parse_dump(fixture("dump.json"))
    assert entries[".tool"].type == "file"
    assert entries[".tool"].perm == 0o755 == 493
    assert entries[".mine"].perm == 0o644
    assert entries[".mine"].contents == "mine\n"
    assert entries[".tmplfile"].contents == "TMPL\nMORE\n"
    assert entries[".dir"].type == "dir"
    assert entries[".dir"].contents is None
    assert entries[".link"].type == "symlink"
    assert entries[".link"].linkname == ".mine"
    assert entries["hello.sh"].type == "script"
    assert entries["hello.sh"].condition == "onchange"
    assert entries["hello.sh"].perm is None


@pytest.mark.parametrize(
    "text",
    [
        "{",
        '{".x": {"type": "file"}}',
        '{".x": {"name": ".x"}}',
        '{".x": {"name": ".x", "type": "file", "perm": "644"}}',
        '{".x": {"name": ".x", "type": "file", "perm": true}}',
        '{".x": {"name": ".x", "type": "file", "contents": 1}}',
    ],
)
def test_dump_malformed(text):
    with pytest.raises(ChezmoiUnavailable, match="dump"):
        parse_dump(text)


# --- State dump ---------------------------------------------------------------


def test_state_dump_fixture():
    states = parse_state_dump(fixture("state-dump.json"))
    dump = parse_dump(fixture("dump.json"))
    mine = states[HOME / ".mine"]
    assert mine.type == "file"
    assert mine.mode == 0o644
    assert mine.contents_sha256 is not None
    assert re.fullmatch(r"[0-9a-f]{64}", mine.contents_sha256)
    # The you→ test of DEC-14: the target state's hash equals the recorded one
    # for `.mine`, and not for `.both file`, whose source changed too.
    assert sha256(dump[".mine"].contents) == mine.contents_sha256
    assert sha256(dump[".both file"].contents) != states[HOME / ".both file"].contents_sha256
    assert states[HOME / ".dir"].contents_sha256 is None
    assert states[HOME / ".link"].type == "symlink"
    assert states[HOME / ".link"].mode is None


def sha256(text: str | None) -> str:
    assert text is not None
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}",
        '{"entryState": []}',
        '{"entryState": {"/h/.x": {}}}',
        '{"entryState": {"/h/.x": {"type": "file", "mode": "420"}}}',
    ],
)
def test_state_dump_malformed(text):
    with pytest.raises(ChezmoiUnavailable, match="state dump"):
        parse_state_dump(text)


# --- Dump-config --------------------------------------------------------------


def test_dump_config_fixture():
    config = parse_dump_config(fixture("dump-config.json"))
    assert config.source_dir == ROOT / "source"
    assert config.working_tree == ROOT / "source"
    assert config.dest_dir == HOME
    assert (config.auto_commit, config.auto_push) == (False, False)


@pytest.mark.parametrize(
    ("autocommit", "autopush", "auto_commit"),
    [(True, False, True), (False, True, True), (True, True, True), (False, False, False)],
)
def test_dump_config_autopush_counts_as_autocommit(autocommit, autopush, auto_commit):
    data = json.loads(fixture("dump-config.json"))
    data["git"]["autocommit"] = autocommit
    data["git"]["autopush"] = autopush
    config = parse_dump_config(json.dumps(data))
    assert (config.auto_commit, config.auto_push) == (auto_commit, autopush)


@pytest.mark.parametrize("drop", ["sourceDir", "workingTree", "destDir", "git"])
def test_dump_config_missing_key(drop):
    data = json.loads(fixture("dump-config.json"))
    del data[drop]
    with pytest.raises(ChezmoiUnavailable, match="dump-config"):
        parse_dump_config(json.dumps(data))


@pytest.mark.parametrize("text", ["nope", '{"git": {"autocommit": "yes"}}'])
def test_dump_config_malformed(text):
    with pytest.raises(ChezmoiUnavailable, match="dump-config"):
        parse_dump_config(text)


# --- The fixtures themselves --------------------------------------------------


# chezmoi's own default for ejson.keyDir, and a new file's `---` side in a
# diff: the same on every machine.
CHEZMOI_DEFAULT_PATHS = {"/opt/ejson/keys", "/dev/null"}


def test_fixtures_hold_no_machine_paths():
    # An absolute path starts the text, or follows a space or a quote; it ends
    # at a JSON string's escape, as in diff.json's `\n`.
    path = re.compile(r'(?:^|[\s"])(/[^\s"\\]*)', re.MULTILINE)
    for f in FIXTURES.iterdir():
        for p in path.findall(f.read_text()):
            assert p in CHEZMOI_DEFAULT_PATHS or p == "/scratch" or p.startswith("/scratch/"), (
                f.name,
                p,
            )


# --- Live: the installed chezmoi ----------------------------------------------


@requires_chezmoi
async def test_live_reads(scratch: Scratch):
    build_drift(scratch)
    cz = scratch.chezmoi()
    home = scratch.home

    rows = {r.target: r.columns for r in await cz.status()}
    assert rows == {
        home / ".both file": "MM",
        home / ".gone": "DA",
        home / ".mine": "MM",
        home / ".tmplfile": " M",
        home / ".tool": " M",
        home / "hello.sh": " R",
    }
    assert [r.target for r in await cz.status([home / ".mine"])] == [home / ".mine"]

    managed = await cz.managed()
    assert managed[".both file"].absolute == home / ".both file"
    assert managed[".both file"].source_absolute == scratch.source / "dot_both file"
    assert managed[".tmplfile"].source_relative == "dot_tmplfile.tmpl"
    assert list(await cz.managed(["templates"])) == [".tmplfile"]
    assert set(await cz.managed(["dirs", "symlinks"])) == {".dir", ".link"}

    dump = await cz.dump()
    assert dump[".tool"].perm == 0o755
    assert dump[".link"].linkname == ".mine"
    assert dump["hello.sh"].condition == "onchange"
    assert dump[".dir"].type == "dir"
    only = await cz.dump([home / ".mine", home / ".both file"])
    assert set(only) == {".mine", ".both file"}

    states = await cz.entry_state()
    assert sha256(only[".mine"].contents) == states[home / ".mine"].contents_sha256
    assert sha256(only[".both file"].contents) != states[home / ".both file"].contents_sha256

    config = await cz.dump_config()
    assert (config.source_dir, config.working_tree, config.dest_dir) == (
        scratch.source,
        scratch.source,
        home,
    )
    assert (config.auto_commit, config.auto_push) == (False, False)
    scratch.set_auto_commit(True)
    assert (await cz.dump_config()).auto_commit is True


@requires_chezmoi
async def test_live_reads_of_an_empty_setup(scratch: Scratch):
    cz = scratch.chezmoi()
    assert await cz.status() == []
    assert await cz.managed() == {}
    assert await cz.dump() == {}
    assert await cz.entry_state() == {}


# --- Recording ----------------------------------------------------------------


@requires_chezmoi
@pytest.mark.skipif(
    os.environ.get("CHEZTUI_RECORD_FIXTURES") != "1", reason="set CHEZTUI_RECORD_FIXTURES=1"
)
def test_record_fixtures(scratch: Scratch, monkeypatch: pytest.MonkeyPatch):
    # dump-config prints the temp dir: keep it in the scratch root.
    (scratch.root / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch.root / "tmp"))
    build_drift(scratch)
    outputs = {
        "status.txt": ["status", "--path-style=absolute"],
        "managed.json": ["managed", "--format=json", "--path-style=all"],
        "managed-templates.json": [
            "managed",
            "--format=json",
            "--path-style=all",
            "--include=templates",
        ],
        "dump.json": ["dump", "--format=json"],
        "state-dump.json": ["state", "dump", "--format=json"],
        "dump-config.json": ["dump-config", "--format=json"],
    }
    for name, args in outputs.items():
        text = scratch.run(*args).stdout.replace(str(scratch.root), str(ROOT))
        (FIXTURES / name).write_text(text)
    # The Drift detail's diff for every status row, keyed by its /scratch target.
    diffs = {}
    for row in parse_status(scratch.run("status", "--path-style=absolute").stdout):
        done = scratch.run(*DIFF_ARGS, str(row.target))
        diffs[str(row.target).replace(str(scratch.root), str(ROOT))] = done.stdout.replace(
            str(scratch.root), str(ROOT)
        )
    (FIXTURES / "diff.json").write_text(json.dumps(diffs, indent=2, sort_keys=True) + "\n")
    test_fixtures_hold_no_machine_paths()
