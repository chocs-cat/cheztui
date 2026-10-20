"""Ops engine and internal editor contracts (spec §8)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cheztui.cli import main
from cheztui.config import SecretsConfig
from cheztui.edit import (
    SpecError,
    apply_spec,
    parse_spec,
    sha256,
    spec_from_json,
    split_lines,
    unified_diff,
)
from cheztui.secrets import Masker


@pytest.mark.parametrize(
    ("op", "text", "expected"),
    [
        ({"op": "replace", "old": "a", "new": "b"}, "abc", "bbc"),
        ({"op": "delete", "old": "a"}, "abc", "bc"),
        ({"op": "insert_after", "anchor": "a", "text": "X"}, "abc", "aXbc"),
        ({"op": "insert_before", "anchor": "a", "text": "X"}, "abc", "Xabc"),
        ({"op": "append", "text": "X"}, "abc", "abc\nX"),
        ({"op": "append", "text": "X"}, "abc\n", "abc\nX"),
        ({"op": "append", "text": "X"}, "", "X"),
        ({"op": "prepend", "text": "X"}, "abc", "Xabc"),
        ({"op": "replace", "old": "a", "new": "b", "count": 2}, "aa", "bb"),
        ({"op": "replace", "old": "a", "new": "b", "count": "all"}, "aaa", "bbb"),
        (
            {"op": "regex_replace", "pattern": "^A.(b) # comment", "repl": r"\1", "flags": "msix"},
            "a\nb",
            "b",
        ),
        ({"op": "replace", "old": "a", "new": "b"}, "a\r\nc\r\n", "b\r\nc\r\n"),
    ],
)
def test_ops(op, text, expected):
    assert apply_spec(spec_from_json([op]), text, "~/.foo") == expected


@pytest.mark.parametrize("count", [0, -1, True, "1", None])
def test_bad_count(count):
    with pytest.raises(SpecError) as e:
        apply_spec(
            spec_from_json([{"op": "replace", "old": "a", "new": "b", "count": count}]),
            "a",
            "~/.foo",
        )
    assert str(e.value) == "op 1 (replace): 'count' must be a positive integer or \"all\""


@pytest.mark.parametrize(
    ("op", "text", "error"),
    [
        (1, "a", "op 1 is not an object"),
        ({"op": "frob"}, "a", "op 1 (frob): unknown op 'frob'"),
        ({"op": "replace", "old": "a"}, "a", "op 1 (replace): needs 'old' and 'new'"),
        ({"op": "delete"}, "a", "op 1 (delete): needs 'old'"),
        ({"op": "insert_after"}, "a", "op 1 (insert_after): needs 'anchor' and 'text'"),
        ({"op": "insert_before"}, "a", "op 1 (insert_before): needs 'anchor' and 'text'"),
        ({"op": "append"}, "a", "op 1 (append): needs 'text'"),
        ({"op": "prepend"}, "a", "op 1 (prepend): needs 'text'"),
        ({"op": "regex_replace"}, "a", "op 1 (regex_replace): needs 'pattern' and 'repl'"),
        (
            {"op": "replace", "old": "z", "new": "b"},
            "a",
            "op 1 (replace): no match found (expected 1)",
        ),
        (
            {"op": "replace", "old": "z", "new": "b", "count": "all"},
            "a",
            "op 1 (replace): no match found",
        ),
        (
            {"op": "replace", "old": "a", "new": "b"},
            "aa",
            "op 1 (replace): matched 2 time(s) but expected 1 - make the match string "
            'more specific, or set "count"',
        ),
        (
            {"op": "replace", "old": "a", "new": "a"},
            "a",
            "ops produced no change to ~/.foo - the spec is probably wrong",
        ),
        (
            {"op": "regex_replace", "pattern": "a", "repl": "b", "flags": "q"},
            "a",
            "op 1 (regex_replace): unknown regex flag 'q' (use m, s, i, x)",
        ),
        (
            {"op": "regex_replace", "pattern": "[", "repl": "b"},
            "a",
            "op 1 (regex_replace): bad regex: unterminated character set at position 0",
        ),
        (
            {"op": "regex_replace", "pattern": "a", "repl": r"\9"},
            "a",
            "op 1 (regex_replace): bad replacement: invalid group reference 9 at position 1",
        ),
    ],
)
def test_messages(op, text, error):
    with pytest.raises(SpecError) as e:
        apply_spec(spec_from_json([op]), text, "~/.foo")
    assert str(e.value) == error


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("replace", {"old": "a", "new": "b"}),
        ("delete", {"old": "a"}),
        ("insert_after", {"anchor": "a", "text": "b"}),
        ("insert_before", {"anchor": "a", "text": "b"}),
        ("append", {"text": "b"}),
        ("prepend", {"text": "b"}),
        ("regex_replace", {"pattern": "a", "repl": "b", "flags": ""}),
    ],
)
def test_non_string_fields(kind, fields):
    for field in fields:
        with pytest.raises(SpecError) as e:
            apply_spec(spec_from_json([{"op": kind, **fields, field: 4}]), "a", "~/.foo")
        assert str(e.value) == f"op 1 ({kind}): '{field}' must be a string"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (1, "edit spec must be a JSON object or list of ops"),
        ({}, "edit spec has no 'ops' list"),
        ([], "edit spec has no 'ops' list"),
        ({"ops": "x"}, "edit spec has no 'ops' list"),
        ({"ops": [1], "skip_if_contains": 1}, "'skip_if_contains' must be a string"),
    ],
)
def test_spec_errors(value, message):
    with pytest.raises(SpecError, match=message):
        spec_from_json(value)


def test_parse_and_skip():
    with pytest.raises(SpecError, match="edit spec is not valid JSON:"):
        parse_spec("{")
    for skip, expected in [("a", None), ("z", "ab"), ("", "ab"), (None, "ab")]:
        spec = parse_spec(
            json.dumps(
                {"ops": [{"op": "append", "text": "b"}], "skip_if_contains": skip, "ignored": 3}
            )
        )
        assert apply_spec(spec, "a\n", "~/.foo") == (None if expected is None else "a\nb")
    assert apply_spec(spec_from_json({"ops": [1], "skip_if_contains": "a"}), "a", "a") is None
    spec = spec_from_json(
        [{"op": "replace", "old": "a", "new": "b"}, {"op": "replace", "old": "b", "new": "c"}]
    )
    assert apply_spec(spec, "a", "a") == "c"


@pytest.mark.parametrize(
    ("old", "new"),
    [("a", "b"), ("a\n", "a"), ("a", "a\n"), ("a\rleak\n\x0cleak\nb", "a\rleak\n\x0cleak\nc")],
)
def test_diff_and_whole_mask(old, new):
    diff = unified_diff(old, new, ".foo")
    assert diff.startswith("--- a/.foo\n+++ b/.foo\n@@")
    assert "\\ No newline at end of file\n" in diff
    masked = (
        Masker(SecretsConfig(), Path("/")).mask(diff, Path("/.foo"), diff=True, whole=True).text
    )
    # Every line past the headers is a hunk header, the marker, or masked whole.
    for line in split_lines(masked)[2:]:
        assert (
            line.startswith("@@")
            or line == "\\ No newline at end of file\n"
            or (line[0] in "+- " and line[1:] == "********\n")
        )
    assert "leak" not in masked
    assert unified_diff(old, old, ".foo") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("a", ["a"]),
        ("a\n", ["a\n"]),
        ("a\r\nb", ["a\r\n", "b"]),
        ("a\rb\x0cc\n", ["a\rb\x0cc\n"]),
    ],
)
def test_split_lines_at_newline_alone(text, expected):
    # git ends a diff line at `\n` only (DEC-40).
    assert split_lines(text) == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_diff_matches_git(tmp_path):
    # #27: a form feed and a lone `\r` mid-file are inside their lines, as git reads them.
    old = "a\n\x0cpage two\nsecret=abc\rmore\nz\n"
    new = old.replace("z\n", "y\n")
    diff = unified_diff(old, new, "x")
    assert diff.count("\n@@ -") == 1
    assert "No newline" not in diff
    (tmp_path / "a").write_bytes(old.encode())
    (tmp_path / "b").write_bytes(new.encode())
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    done = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", "a", "b"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    git = done.stdout.decode()
    assert diff[diff.index("@@") :] == git[git.index("@@") :]


def test_editor_writes_same_inode(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"a\r\n")
    inode = path.stat().st_ino
    payload = tmp_path / "spec"
    payload.write_text(
        json.dumps({"spec": [{"op": "replace", "old": "a", "new": "b"}], "sha256": sha256("b\r\n")})
    )
    monkeypatch.setenv("CHEZTUI_EDIT_SPEC", str(payload))
    assert main(["_editor", str(path)]) == 0
    assert path.read_bytes() == b"b\r\n"
    assert path.stat().st_ino == inode


@pytest.mark.parametrize(
    "case", ["hash", "skip", "op", "utf8", "env", "zero", "two", "missing", "bad_json"]
)
def test_editor_refusals(tmp_path, monkeypatch, capsys, case):
    path = tmp_path / "file"
    original = b"\xff" if case == "utf8" else b"a\r\n"
    path.write_bytes(original)
    spec = {"ops": [{"op": "append", "text": "b"}]}
    if case == "skip":
        spec["skip_if_contains"] = "a"
    if case == "op":
        spec["ops"] = [1]
    payload = tmp_path / "spec"
    payload.write_text("{" if case == "bad_json" else json.dumps({"spec": spec, "sha256": "wrong"}))
    monkeypatch.setenv("CHEZTUI_EDIT_SPEC", str(payload))
    if case == "missing":
        payload.unlink()
    if case == "env":
        monkeypatch.delenv("CHEZTUI_EDIT_SPEC")
    args = [] if case == "zero" else [str(path), str(path)] if case == "two" else [str(path)]
    assert main(["_editor", *args]) == 1
    assert path.read_bytes() == original
    assert capsys.readouterr().err.startswith("cheztui _editor: ")


def test_editor_hidden(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "_editor" not in capsys.readouterr().out
