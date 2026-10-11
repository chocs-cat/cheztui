"""The write path (spec §5 Write flow; DEC-8, DEC-23, DEC-27 to DEC-30).

Unit tests use `tests.fake_write`; the live tests run the real chezmoi and
git against the scratch setup, never with autoPush or a remote.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from cheztui.chezmoi import Chezmoi, ChezmoiFailed, ChezmoiUnavailable
from cheztui.config import Config
from cheztui.git import GitFailed
from cheztui.ops import (
    CommandResult,
    Preflight,
    WriteCommand,
    WriteOps,
    WritePlan,
    WriteResult,
    mask_diagnostic,
    short_error,
)
from cheztui.runner import Completed
from cheztui.secrets import MASK, Masker
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_write import FakeWrite, H, S

SECRET = "ghp_" + "S" * 36  # synthetic


# --- Pre-flight -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("auto_commit", "auto_push", "stray", "blocked", "warns"),
    [
        (True, False, [], False, False),
        (True, False, ["stray file"], True, False),
        (False, True, ["stray file"], True, False),  # DEC-23: autoPush alone commits
        (True, True, ["stray file"], True, False),
        (False, False, ["stray file"], False, True),
        (False, False, [], False, False),
    ],
)
async def test_preflight(auto_commit, auto_push, stray, blocked, warns):
    fake = FakeWrite(auto_commit=auto_commit, auto_push=auto_push, stray=stray)
    pf = await fake.ops().preflight()
    assert pf == Preflight(S, H, S, auto_commit or auto_push, tuple(stray))
    assert (pf.blocked, pf.warns) == (blocked, warns)
    # dump-config first, then porcelain in its working tree; nothing writes.
    assert [c[:4] for c in fake.log] == [
        ("chezmoi", "--no-tty", "dump-config", "--format=json"),
        ("git", "-C", str(S), "status"),
    ]


async def test_preflight_uses_the_effective_working_tree():
    fake = FakeWrite(work_tree=Path("/repo"), source=Path("/repo/home"))
    pf = await fake.ops(source=Path("/elsewhere")).preflight()
    assert (pf.source_dir, pf.working_tree) == (Path("/repo/home"), Path("/repo"))
    assert fake.log[-1][:3] == ("git", "-C", "/repo")


async def test_failed_dump_config_is_masked_whole_and_never_clean():
    fake = FakeWrite()
    fake.fail("dump-config", f"template: line 1: token={SECRET}\nrendered {SECRET}\n".encode())
    with pytest.raises(ChezmoiFailed) as info:
        await fake.ops().preflight()
    e = info.value
    assert SECRET not in e.stderr + str(e) + repr(e)
    assert (e.returncode, e.last_line) == (1, MASK)
    assert e.__context__ is None
    assert e.__cause__ is None
    assert len(fake.log) == 1  # no git status, no write


async def test_failed_git_status_raises_masked():
    fake = FakeWrite(auto_commit=True)
    fake.fail("status", f"fatal: {SECRET}\n".encode(), 128)
    with pytest.raises(GitFailed) as info:
        await fake.ops().preflight()
    assert SECRET not in str(info.value) + info.value.stderr
    assert info.value.returncode == 128
    assert info.value.__context__ is None


async def test_unreadable_dump_config_is_chezmoi_unavailable():
    fake = FakeWrite()
    fake.reply(
        lambda argv, env: Completed(argv, 0, b"not json", b"") if "dump-config" in argv else None
    )
    with pytest.raises(ChezmoiUnavailable):
        await fake.ops().preflight()


def test_same_paths():
    pf = Preflight(S, H, S, True, ())
    assert pf.same_paths(Preflight(S, H, S, False, ("x",)))
    for other in (
        Preflight(H, H, S, True, ()),
        Preflight(S, S, S, True, ()),
        Preflight(S, H, H, True, ()),
    ):
        assert not pf.same_paths(other)


# --- Plan and result data -------------------------------------------------------


def test_command_display_quotes_and_argv_keeps_spaces():
    cz = Chezmoi(config=Path("/c d/chezmoi.toml"), source=Path("/s 'q'"))
    target = Path("/h/a file; rm -rf")
    argv = (*cz.argv("apply", "--force", "--recursive=false"), str(target))
    cmd = WriteCommand("apply", (target,), argv, False)
    assert shlex.split(cmd.display) == list(argv)
    assert "'/h/a file; rm -rf'" in cmd.display
    assert str(target) in cmd.argv
    assert "/c d/chezmoi.toml" in cmd.argv


def test_plan_states():
    clean = Preflight(S, H, S, True, ())
    dirty = Preflight(S, H, S, True, ("x",))
    apply = WriteCommand("apply", (H / "a",), ("chezmoi", "apply"), False)
    merge = WriteCommand("merge", (H / "a",), ("chezmoi", "merge"), True)
    assert not WritePlan(clean).blocked
    assert WritePlan(dirty).blocked
    assert WritePlan(clean, error="boom").blocked
    assert WritePlan(clean, commands=(apply, merge)).needs_confirmation
    assert not WritePlan(clean, commands=(merge,)).needs_confirmation


def test_result_ok():
    cmd = WriteCommand("apply", (H / "a",), ("chezmoi", "apply"), False)
    assert WriteResult((CommandResult(cmd, "ok"),)).ok
    assert WriteResult((CommandResult(cmd, "dry run"),)).ok
    assert not WriteResult((CommandResult(cmd, "failed"),)).ok
    assert not WriteResult(refused="blocked").ok


def test_diagnostics():
    assert short_error("", "apply", 2) == "chezmoi apply exited 2"
    assert short_error("first\n  last line  \n\n", "apply", 1) == "last line"
    text = f'token = "{SECRET}"\nplain\n'
    assert mask_diagnostic(None, text, whole=False) == f"{MASK}\n{MASK}\n"
    masker = Masker(Config(Path("/c")).secrets, H)
    assert mask_diagnostic(masker, text, whole=False) == f'token = "{MASK}"\nplain\n'
    assert mask_diagnostic(masker, text, whole=True) == f"{MASK}\n{MASK}\n"


# --- Live: the scratch setup ----------------------------------------------------


@requires_chezmoi
async def test_live_preflight_working_tree_differs_from_source(scratch: Scratch):
    source = scratch.source / "home"
    source.mkdir()
    scratch.config.write_text(f'workingTree = "{scratch.source}"\n[git]\n    autoCommit = true\n')
    (scratch.source / "README").write_text("outside the source dir\n")
    ops = WriteOps(
        Chezmoi(config=scratch.config, source=source, destination=scratch.home),
        Config(scratch.root / "cheztui.toml"),
    )
    pf = await ops.preflight()
    assert (pf.source_dir, pf.working_tree, pf.dest_dir) == (source, scratch.source, scratch.home)
    assert pf.stray == ("README",)
    assert pf.blocked
    scratch.commit_all("readme")
    assert not (await ops.preflight()).blocked


@requires_chezmoi
async def test_live_preflight_auto_commit_off_warns(scratch: Scratch):
    (scratch.source / "stray").write_text("x\n")
    pf = await WriteOps(scratch.chezmoi(), Config(scratch.root / "c.toml")).preflight()
    assert pf.warns
    assert not pf.blocked
    assert pf.stray == ("stray",)
