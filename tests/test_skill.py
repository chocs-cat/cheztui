"""The agent skill against the real CLI (spec §8 Skill, §11; DEC-39).

Every `cheztui` command line in the skill's `sh` blocks must parse with the
CLI's own parser, so a renamed flag fails here instead of misleading an
agent. The flow the skill teaches then runs end to end as a subprocess in
the scratch setup, holding §7's and §8's JSON shapes and exit codes.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from cheztui import cli
from tests.conftest import Scratch, requires_chezmoi

SKILL = Path(__file__).resolve().parent.parent / "skills" / "cheztui" / "SKILL.md"


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        assert sep, line
        fields[key.strip()] = value.strip()
    return fields


def command_lines(text: str) -> list[str]:
    """Every line starting `cheztui ` inside a ```sh block."""
    blocks = re.findall(r"^ *```sh\n(.*?)^ *```$", text, re.M | re.S)
    return [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip().startswith("cheztui ")
    ]


def argv(line: str) -> list[str]:
    """The line's arguments, cut at a heredoc, redirect or pipe."""
    args = shlex.split(line, comments=True)[1:]
    for i, arg in enumerate(args):
        if arg.startswith(("<", ">", "|")):
            return args[:i]
    return args


def test_frontmatter():
    fields = frontmatter(SKILL.read_text())
    assert fields["name"] == "cheztui"
    assert fields["description"]


def test_the_listing_finds_the_skill_commands():
    commands = {
        cli.build_parser().parse_args(argv(line)).command
        for line in command_lines(SKILL.read_text())
    }
    assert {"status", "verify", "edit", "add"} <= commands


@pytest.mark.parametrize("line", command_lines(SKILL.read_text()))
def test_every_command_line_parses(line):
    try:
        cli.build_parser().parse_args(argv(line))
    except SystemExit as e:
        pytest.fail(f"{line!r} doesn't parse (exit {e.code})")


def test_argv_cuts_at_a_heredoc():
    assert argv("cheztui edit ~/.zshrc --ops - --json <<'EOF'") == [
        "edit",
        "~/.zshrc",
        "--ops",
        "-",
        "--json",
    ]
    assert argv("cheztui status --json  # comment") == ["status", "--json"]


# --- the flow, end to end ------------------------------------------------------------

STATUS_KEYS = {"target", "source", "direction", "default_fix", "columns", "tags"}
EDIT_KEYS = {
    "target",
    "source",
    "template",
    "encrypted",
    "skipped",
    "diff",
    "commit",
    "pushed",
    "verified",
}
ADD_KEYS = EDIT_KEYS - {"skipped"}


def cheztui(s: Scratch, *args: str | Path) -> tuple[int, dict]:
    # `-P` keeps the cwd, which holds a planted module, off sys.path.
    done = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "cheztui",
            "--chezmoi-config",
            str(s.config),
            "--source",
            str(s.source),
            "--destination",
            str(s.home),
            *map(str, args),
        ],
        cwd=s.root,
        capture_output=True,
        text=True,
    )
    return done.returncode, json.loads(done.stdout)


@requires_chezmoi
def test_the_skill_flow(scratch: Scratch):
    (scratch.root / "difflib.py").write_text("raise RuntimeError('planted module imported')\n")
    clean = scratch.home / ".zshrc"
    clean.write_text("alias ll='ls -l'\n")
    drifted = scratch.home / ".gitconfig"
    drifted.write_text("[user]\n")
    scratch.run("add", str(clean), str(drifted))
    scratch.commit_all("initial")
    scratch.set_auto_commit(True)
    drifted.write_text("[user]\n\tname = changed here\n")
    spec = scratch.root / "ops.json"
    spec.write_text(
        json.dumps(
            {
                "skip_if_contains": "alias la=",
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": "alias ll='ls -l'\n",
                        "text": "alias la='ls -a'\n",
                    }
                ],
            }
        )
    )

    code, status = cheztui(scratch, "status", "--json")
    assert code == 0
    [row] = status["rows"]
    assert set(row) == STATUS_KEYS
    assert row["target"] == str(drifted)
    assert row["direction"] == "you"

    code, refused = cheztui(scratch, "edit", drifted, "--ops", spec, "--json")
    assert code == 1
    assert set(refused) == {"error"}

    code, dry = cheztui(scratch, "edit", clean, "--ops", spec, "--dry-run", "--json")
    assert code == 0
    assert set(dry) == EDIT_KEYS
    assert dry["commit"] is None
    assert dry["verified"] is None
    assert "+alias la='ls -a'\n" in dry["diff"]

    code, edited = cheztui(scratch, "edit", clean, "--ops", spec, "--json")
    assert code == 0
    assert set(edited) == EDIT_KEYS
    assert re.fullmatch(r"[0-9a-f]{40}", edited["commit"]["sha"])
    assert edited["pushed"] is False
    assert edited["verified"] is True
    assert clean.read_text() == "alias ll='ls -l'\nalias la='ls -a'\n"

    new = scratch.home / ".config" / "tool.toml"
    new.write_text("key = 1\n")
    code, would_add = cheztui(scratch, "add", new, "--dry-run", "--json")
    assert code == 0
    assert set(would_add) == ADD_KEYS
    assert would_add["source"] is None

    code, added = cheztui(scratch, "add", new, "--json")
    assert code == 0
    assert added["verified"] is True

    code, verify = cheztui(scratch, "verify", "--json")
    assert code == 1
    assert verify == {"verified": False, "failing": [str(drifted)]}
