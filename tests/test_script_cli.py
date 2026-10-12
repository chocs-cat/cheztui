"""Human and JSON edit/add CLI contracts (spec §7, §8)."""

import io
import json
import re

import pytest

from cheztui.cli import main
from tests.conftest import requires_chezmoi
from tests.test_script_ops import SPEC, setup_file


def invoke(s, *args):
    return main(
        [
            "--chezmoi-config",
            str(s.config),
            "--source",
            str(s.source),
            "--destination",
            str(s.home),
            *map(str, args),
        ]
    )


def spec_file(s):
    path = s.root / "ops.json"
    path.write_text(json.dumps(SPEC.raw))
    return path


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("mode", ["commit", "dry", "skip"])
def test_edit_output(scratch, capsys, as_json, mode):
    target = setup_file(scratch)
    scratch.set_auto_commit(True)
    spec = spec_file(scratch)
    if mode == "skip":
        spec.write_text(json.dumps({"skip_if_contains": "old", "ops": [1]}))
    flags = [*(["--json"] if as_json else []), *(["--dry-run"] if mode == "dry" else [])]
    assert invoke(scratch, "edit", target, "--ops", spec, *flags) == 0
    out, err = capsys.readouterr()
    assert err == ""
    if as_json:
        payload = json.loads(out)
        assert set(payload) == {
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
        assert payload["target"] == str(target)
        assert payload["source"] == str(scratch.source / "dot_foo")
        assert payload["template"] is False
        assert payload["encrypted"] is False
        assert payload["skipped"] is (mode == "skip")
        if mode == "commit":
            assert payload["commit"] == {
                "sha": scratch.git("rev-parse", "HEAD").stdout.strip(),
                "subject": scratch.git("log", "-1", "--format=%s").stdout.strip(),
            }
            assert payload["pushed"] is False
            assert payload["verified"] is True
        else:
            assert payload["commit"] is None
            assert payload["pushed"] is None
            assert payload["verified"] is None
        if mode == "skip":
            assert payload["diff"] == ""
        else:
            assert payload["diff"] == "--- a/.foo\n+++ b/.foo\n@@ -1 +1 @@\n-old\n+new\n"
    elif mode == "commit":
        assert re.search(r'edited ~/.foo → [a-f0-9]{7} "[^"\n]+" \(not pushed\)\n$', out)
        assert "-old\n+new\n" in out
    elif mode == "dry":
        assert out.endswith("would edit ~/.foo (dry run)\n")
        assert "-old\n+new\n" in out
    else:
        assert out == "skipped ~/.foo: already contains 'old', leaving unchanged\n"


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
def test_template_note_and_stdin(scratch, capsys, monkeypatch, as_json):
    target = setup_file(scratch, template=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SPEC.raw)))
    assert (
        invoke(scratch, "edit", target, "--ops", "-", "--dry-run", *(["--json"] if as_json else []))
        == 0
    )
    out, err = capsys.readouterr()
    assert err == "note: ~/.foo is a template; the applied result differs from the edit\n"
    if as_json:
        assert json.loads(out)["template"] is True
    else:
        assert out.endswith("would edit ~/.foo (dry run)\n")


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
def test_refusals(scratch, capsys, as_json, dirty):
    target = setup_file(scratch)
    scratch.set_auto_commit(True)
    if dirty:
        (scratch.source / "stray").write_text("stray")
        error = "the source tree has uncommitted changes"
    else:
        target = scratch.home / "unmanaged"
        error = "~/unmanaged isn't managed by chezmoi; use cheztui add to add it"
    assert (
        invoke(
            scratch, "edit", target, "--ops", spec_file(scratch), *(["--json"] if as_json else [])
        )
        == 1
    )
    out, err = capsys.readouterr()
    if as_json:
        assert json.loads(out) == {"error": error, **({"stray": ["stray"]} if dirty else {})}
        assert err == ""
    else:
        assert out == ""
        assert err == f"cheztui: {error}\n" + ("  stray\n" if dirty else "")


@pytest.mark.parametrize("case", ["missing", "invalid", "utf8"])
@pytest.mark.parametrize("as_json", [False, True])
def test_bad_spec_before_chezmoi(scratch, capsys, monkeypatch, case, as_json):
    spec = spec_file(scratch)
    if case == "missing":
        spec.unlink()
    else:
        spec.write_bytes(b"{" if case == "invalid" else b"\xff")

    async def must_not_run(self):
        raise AssertionError("version check before spec refusal")

    monkeypatch.setattr("cheztui.chezmoi.Chezmoi.check_version", must_not_run)
    assert (
        invoke(
            scratch, "edit", scratch.home / ".foo", "--ops", spec, *(["--json"] if as_json else [])
        )
        == 1
    )
    out, err = capsys.readouterr()
    if as_json:
        assert set(json.loads(out)) == {"error"}
        assert err == ""
    else:
        assert out == ""
        assert err.startswith("cheztui: ")


@pytest.mark.parametrize("case", ["config", "missing_binary"])
def test_startup_errors_no_json(scratch, capsys, monkeypatch, case):
    if case == "config":
        (scratch.home / ".config" / "cheztui").mkdir()
        (scratch.home / ".config" / "cheztui" / "config.toml").write_text("[invalid")
        expected = 2
    else:
        monkeypatch.setenv("PATH", str(scratch.root / "no-binaries"))
        expected = 3
    assert (
        invoke(scratch, "edit", scratch.home / ".foo", "--ops", spec_file(scratch), "--json")
        == expected
    )
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("cheztui: ")


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
def test_stray_warning_with_auto_commit_off(scratch, capsys, as_json):
    target = setup_file(scratch)
    (scratch.source / "stray").write_text("stray")
    assert (
        invoke(
            scratch, "edit", target, "--ops", spec_file(scratch), *(["--json"] if as_json else [])
        )
        == 0
    )
    out, err = capsys.readouterr()
    assert err == "warning: uncommitted files in the source tree:\n  stray\n"
    if as_json:
        assert json.loads(out)["commit"] is None
    else:
        assert out.endswith("edited ~/.foo (not committed)\n")


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("case", ["hash", "verify"])
def test_failure_after_write(scratch, capsys, monkeypatch, as_json, case):
    from pathlib import Path

    from cheztui.chezmoi import Chezmoi
    from cheztui.ops import WriteOps

    target = setup_file(scratch)
    if case == "hash":
        execute = WriteOps.execute

        async def changed_payload(self, plan, **kwargs):
            path = Path(dict(plan.commands[0].env)["CHEZTUI_EDIT_SPEC"])
            payload = json.loads(path.read_text())
            payload["sha256"] = "changed"
            path.write_text(json.dumps(payload))
            return await execute(self, plan, **kwargs)

        monkeypatch.setattr(WriteOps, "execute", changed_payload)
        expected = "cheztui _editor: edit result doesn't match the expected sha256"
    else:

        async def unverified(self, target):
            return False

        monkeypatch.setattr(Chezmoi, "verify", unverified)
        expected = "~/.foo doesn't verify after the edit"
    assert (
        invoke(
            scratch, "edit", target, "--ops", spec_file(scratch), *(["--json"] if as_json else [])
        )
        == 1
    )
    out, err = capsys.readouterr()
    if as_json:
        payload = json.loads(out)
        assert payload["error"] == expected
        assert payload["verified"] is False
        assert set(payload) == {
            "target",
            "source",
            "template",
            "encrypted",
            "diff",
            "commit",
            "pushed",
            "verified",
            "skipped",
            "error",
        }
        assert err == ""
    else:
        assert err.endswith(f"cheztui: {expected}\n")
        assert not out.endswith("(not committed)\n")
    assert target.read_bytes() == (b"old\n" if case == "hash" else b"new\n")


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_add_output(scratch, capsys, as_json, dry_run):
    scratch.set_auto_commit(True)
    target = scratch.home / "new"
    target.write_text("new\n")
    flags = [*(["--json"] if as_json else []), *(["--dry-run"] if dry_run else [])]
    assert invoke(scratch, "add", target, "--template", *flags) == 0
    out, err = capsys.readouterr()
    assert err == ""
    if as_json:
        payload = json.loads(out)
        assert set(payload) == {
            "target",
            "source",
            "template",
            "encrypted",
            "diff",
            "commit",
            "pushed",
            "verified",
        }
        assert payload["target"] == str(target)
        assert payload["template"] is True
        assert payload["encrypted"] is False
        assert "+new" in payload["diff"]
        if dry_run:
            assert payload["source"] is None
            assert payload["commit"] is None
            assert payload["pushed"] is None
            assert payload["verified"] is None
        else:
            assert payload["source"] == str(scratch.source / "new.tmpl")
            assert payload["commit"] == {
                "sha": scratch.git("rev-parse", "HEAD").stdout.strip(),
                "subject": scratch.git("log", "-1", "--format=%s").stdout.strip(),
            }
            assert payload["pushed"] is False
            assert payload["verified"] is True
    elif dry_run:
        assert out.endswith("would add ~/new (dry run)\n")
    else:
        assert re.search(r'added ~/new → [a-f0-9]{7} "[^"\n]+" \(not pushed\)\n$', out)


@requires_chezmoi
@pytest.mark.parametrize("as_json", [False, True])
def test_add_refusal(scratch, capsys, as_json):
    target = setup_file(scratch)
    assert invoke(scratch, "add", target, *(["--json"] if as_json else [])) == 1
    out, err = capsys.readouterr()
    expected = "~/.foo is already managed; use cheztui edit"
    if as_json:
        assert json.loads(out) == {"error": expected}
        assert err == ""
    else:
        assert out == ""
        assert err == f"cheztui: {expected}\n"
