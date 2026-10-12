"""Scripted edit/add through the real, isolated write path (spec §8, §11)."""

import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cheztui.chezmoi import parse_dump_config
from cheztui.config import Config, SecretsConfig
from cheztui.edit import spec_from_json
from cheztui.ops import Refusal, ScriptResult, WriteCommand, WriteEntry, WriteOps, WritePlan
from cheztui.runner import Completed, run_subprocess
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_write import FakeWrite, subcommand

SPEC = spec_from_json([{"op": "replace", "old": "old", "new": "new"}])


def setup_file(s: Scratch, content: bytes = b"old\n", *, template: bool = False) -> Path:
    target = s.home / ".foo"
    target.write_bytes(content)
    s.run("add", *(("--template",) if template else ()), str(target))
    s.run("apply", "--force", str(target))
    s.commit_all("initial")
    return target


def write_ops(s: Scratch, *, global_dry_run: bool = False) -> WriteOps:
    return WriteOps(s.chezmoi(dry_run=global_dry_run), Config(s.root / "cheztui.toml"))


def tree(s: Scratch):
    return {
        str(p.relative_to(s.source)): p.read_bytes()
        for p in s.source.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(s.source).parts
    }


def assert_result(result) -> ScriptResult:
    assert isinstance(result, ScriptResult), result
    assert result.error is None, result
    return result


@requires_chezmoi
@pytest.mark.parametrize("auto_commit", [False, True])
async def test_edit_commit_and_verify(scratch, auto_commit):
    target = setup_file(scratch)
    scratch.set_auto_commit(auto_commit)
    if not auto_commit:
        (scratch.source / "stray").write_text("stray")
    result = assert_result(await write_ops(scratch).edit(target, SPEC))
    assert result.verified is True
    assert target.read_bytes() == b"new\n"
    assert result.source is not None
    assert result.source.read_bytes() == b"new\n"
    assert result.template is False
    assert result.encrypted is False
    if auto_commit:
        assert result.commit is not None
        assert result.commit.sha == scratch.git("rev-parse", "HEAD").stdout.strip()
        assert result.commit.subject == scratch.git("log", "-1", "--format=%s").stdout.strip()
        assert result.pushed is False
    else:
        assert result.commit is None
        assert result.pushed is None
        assert result.state == "not committed"
        assert result.warning == ("stray",)


@requires_chezmoi
async def test_edit_upstream_contains_commit(scratch):
    target = setup_file(scratch)
    scratch.set_auto_commit(True)
    scratch.git("branch", "local-upstream")
    scratch.git("branch", "--set-upstream-to=local-upstream")
    cz = scratch.chezmoi()

    async def runner(argv, *, cwd=None, env=None, stdin=None):
        done = await run_subprocess(argv, cwd=cwd, env=env, stdin=stdin)
        if subcommand(argv)[0] == "edit":
            scratch.git("branch", "-f", "local-upstream", "HEAD")
        return done

    cz.runner = runner
    result = assert_result(await WriteOps(cz, Config(scratch.root / "c")).edit(target, SPEC))
    assert result.commit is not None
    assert result.pushed is True


@requires_chezmoi
async def test_template_and_crlf(scratch):
    target = setup_file(scratch, b'old\r\n{{ "rendered" }}\r\n', template=True)
    # add --template may rewrite CRLF; exact source is what the engine must preserve.
    source = next(
        iter((await scratch.chezmoi().managed(targets=[target])).values())
    ).source_absolute
    assert source is not None
    assert source.read_bytes() == b'old\r\n{{ "rendered" }}\r\n'
    result = assert_result(await write_ops(scratch).edit(target, SPEC))
    assert result.template
    assert result.verified
    assert source.read_bytes() == b'new\r\n{{ "rendered" }}\r\n'
    assert target.read_bytes() == b"new\r\nrendered\r\n"


@requires_chezmoi
@pytest.mark.parametrize(
    "case", ["unmanaged", "dir", "symlink", "script", "dirty", "drift", "editor", "utf8"]
)
async def test_edit_refusals_unchanged(scratch, case):
    target = setup_file(scratch)
    if case == "unmanaged":
        target = scratch.home / "unmanaged"
        target.write_text("old")
    elif case == "dir":
        target = scratch.home / "dir"
        target.mkdir()
        scratch.run("add", str(target))
        scratch.commit_all("dir")
    elif case == "symlink":
        target = scratch.home / "link"
        target.symlink_to(".foo")
        scratch.run("add", str(target))
        scratch.commit_all("link")
    elif case == "script":
        source = scratch.source / "run_test.sh"
        source.write_text("#!/bin/sh\ntrue\n")
        scratch.commit_all("script")
        target = scratch.home / "test.sh"
    elif case == "dirty":
        scratch.set_auto_commit(True)
        (scratch.source / "stray").write_text("stray")
    elif case == "drift":
        target.write_text("changed")
    elif case == "editor":
        with scratch.config.open("a") as f:
            f.write('[edit]\ncommand = "should-never-run"\n')
    elif case == "utf8":
        source = scratch.source / "dot_foo"
        source.write_bytes(b"\xff")
        scratch.run("apply", "--force", str(target))
        scratch.commit_all("binary")
    before, head = tree(scratch), scratch.git("rev-parse", "HEAD").stdout
    result = await write_ops(scratch).edit(target, SPEC)
    assert isinstance(result, Refusal), result
    messages = {
        "unmanaged": "isn't managed",
        "dir": "isn't a regular file",
        "symlink": "isn't a regular file",
        "script": "isn't a regular file",
        "dirty": "uncommitted changes",
        "drift": "has drifted",
        "editor": "edit.command is set",
        "utf8": "isn't UTF-8 text",
    }
    assert messages[case] in result.message
    if case == "editor":
        assert isinstance(await write_ops(scratch).edit(target, SPEC, dry_run=True), Refusal)
    assert tree(scratch) == before
    assert scratch.git("rev-parse", "HEAD").stdout == head


@requires_chezmoi
async def test_allow_drift(scratch):
    target = setup_file(scratch)
    target.write_text("changed")
    result = assert_result(await write_ops(scratch).edit(target, SPEC, allow_drift=True))
    assert result.verified
    assert target.read_bytes() == b"new\n"


@requires_chezmoi
@pytest.mark.parametrize("mode", ["dry", "global", "skip"])
async def test_edit_no_write(scratch, mode):
    target = setup_file(scratch)
    scratch.set_auto_commit(True)
    ops = write_ops(scratch, global_dry_run=mode == "global")
    calls = []
    original_runner = ops.chezmoi.runner

    async def runner(argv, **kwargs):
        calls.append(subcommand(argv)[0])
        return await original_runner(argv, **kwargs)

    ops.chezmoi.runner = runner
    spec = spec_from_json({"skip_if_contains": "old", "ops": [1]}) if mode == "skip" else SPEC
    before, head = tree(scratch), scratch.git("rev-parse", "HEAD").stdout
    result = assert_result(await ops.edit(target, spec, dry_run=mode == "dry"))
    assert result.verified is None
    assert result.commit is None
    assert result.pushed is None
    assert "edit" not in calls
    assert tree(scratch) == before
    assert target.read_bytes() == b"old\n"
    assert scratch.git("rev-parse", "HEAD").stdout == head


@requires_chezmoi
@pytest.mark.parametrize("fail", [False, True])
async def test_temp_spec_mode_cleanup_and_hash_guard(scratch, fail):
    target = setup_file(scratch)
    ops = write_ops(scratch)
    paths = []
    before = tree(scratch)

    async def runner(argv, *, cwd=None, env=None, stdin=None):
        if subcommand(argv)[0] == "edit":
            assert env is not None
            path = Path(env["CHEZTUI_EDIT_SPEC"])
            paths.append(path)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            payload = json.loads(path.read_text())
            assert payload["spec"] == SPEC.raw
            if fail:
                payload["sha256"] = "mismatch"
                path.write_text(json.dumps(payload))
        return await run_subprocess(argv, cwd=cwd, env=env, stdin=stdin)

    ops.chezmoi.runner = runner
    result = await ops.edit(target, SPEC)
    assert isinstance(result, ScriptResult)
    assert paths
    assert all(not p.exists() for p in paths)
    if fail:
        assert result.error == "cheztui _editor: edit result doesn't match the expected sha256"
        assert tree(scratch) == before
        assert target.read_bytes() == b"old\n"
    else:
        assert result.verified


def setup_age(scratch):
    identity = scratch.root / "identity"
    try:
        scratch.run("age-keygen", "-o", str(identity))
    except subprocess.CalledProcessError:
        pytest.skip("chezmoi has no builtin age-keygen")
    recipient = next(
        line.split(": ", 1)[1]
        for line in identity.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    scratch.config.write_text(
        f'encryption = "age"\n[age]\nidentity = {json.dumps(str(identity))}\n'
        f"recipient = {json.dumps(recipient)}\n[git]\nautoCommit = false\n"
    )


@requires_chezmoi
async def test_encrypted_edit_masking(scratch):
    setup_age(scratch)
    target = scratch.home / ".secret"
    target.write_bytes(b"old")
    scratch.run("add", "--encrypt", str(target))
    scratch.run("apply", "--force", str(target))
    scratch.commit_all("encrypted")
    ops = write_ops(scratch)
    dry = assert_result(await ops.edit(target, SPEC, dry_run=True))
    assert dry.encrypted
    assert "-old" not in dry.diff
    assert "+new" not in dry.diff
    assert "-********" in dry.diff
    assert "+********" in dry.diff
    assert "\\ No newline" in dry.diff
    revealed = assert_result(await ops.edit(target, SPEC, dry_run=True, reveal=True))
    assert "-old" in revealed.diff
    assert "+new" in revealed.diff
    result = assert_result(await ops.edit(target, SPEC))
    assert result.verified
    assert target.read_bytes() == b"new"
    assert await scratch.chezmoi().decrypt(result.source) == b"new"


async def test_unmanaged_before_preflight_and_no_write():
    fake = FakeWrite(auto_commit=True, stray=["stray"])
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, b"{}", b"") if subcommand(argv)[0] == "managed" else None
        )
    )
    result = await fake.ops().edit("/h/unmanaged", SPEC)
    assert isinstance(result, Refusal)
    assert "isn't managed" in result.message
    assert fake.chezmoi_calls == [("managed", "--format=json", "--path-style=all", "/h/unmanaged")]
    assert fake.writes == []


async def test_command_env_only_and_interactive_override():
    fake = FakeWrite()
    ops = fake.ops()
    pf = await ops.preflight()
    prefix = tuple(ops.chezmoi.argv())
    commands = (
        WriteCommand(
            "edit", (Path("/h/a"),), (*prefix, "edit", "/h/a"), False, (("EDITOR", "own"),)
        ),
        WriteCommand(
            "merge",
            (Path("/h/b"),),
            (*prefix, "merge", "/h/b"),
            True,
            (("EDITOR", "own"), ("ONLY", "yes")),
        ),
    )
    plan = WritePlan(
        pf,
        tuple(WriteEntry(t, None, c.action, True) for c in commands for t in c.targets),
        commands,
        prefix=prefix,
    )
    await ops.execute(
        plan,
        confirmed=True,
        interactive_runner=fake.chezmoi_runner,
        interactive_env={"EDITOR": "interactive"},
    )
    assert [
        env
        for call, env in zip([c for c in fake.log if c[0] == "chezmoi"], fake.envs, strict=True)
        if subcommand(call)[0] in ("edit", "merge")
    ] == [{"EDITOR": "own"}, {"EDITOR": "interactive", "ONLY": "yes"}]


def test_edit_command_parser():
    fixture = Path(__file__).parent / "fixtures" / "chezmoi" / "dump-config.json"
    obj = json.loads(fixture.read_text())
    assert parse_dump_config(json.dumps(obj)).edit_command == ""
    obj["edit"] = {"command": "vim"}
    assert parse_dump_config(json.dumps(obj)).edit_command == "vim"


@requires_chezmoi
async def test_always_mask_only_and_reveal(scratch):
    target = setup_file(scratch, b"password = old\n")
    config = replace(Config(scratch.root / "c"), secrets=SecretsConfig(always_mask=(".foo",)))
    ops = WriteOps(scratch.chezmoi(), config)
    masked = assert_result(await ops.edit(target, SPEC, dry_run=True))
    assert "password" not in masked.diff
    revealed = assert_result(await ops.edit(target, SPEC, dry_run=True, reveal=True))
    assert "password = new" in revealed.diff
    plain = assert_result(await write_ops(scratch).edit(target, SPEC, dry_run=True))
    assert "password = new" in plain.diff


async def test_dirty_edit_preflight_runs_no_writing_command():
    fake = FakeWrite(auto_commit=True, stray=["stray"])

    def reply(argv, env):
        args = subcommand(argv)
        if args[0] == "managed":
            obj = (
                {}
                if args[3] in ("--include=templates", "--include=encrypted", "--include=externals")
                else {"a": {"absolute": "/h/a", "sourceAbsolute": "/s/a", "sourceRelative": "a"}}
            )
            return Completed(argv, 0, json.dumps(obj).encode(), b"")
        return None

    fake.reply(reply)
    result = await fake.ops().edit("/h/a", SPEC)
    assert isinstance(result, Refusal)
    assert result.stray == ("stray",)
    assert fake.writes == []
    assert "execute-template" not in [c[0] for c in fake.chezmoi_calls]
