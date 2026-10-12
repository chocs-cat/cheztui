"""The header's read, `ops.repo_state()`, and the TUI's fake (spec §4 Frame details, §11).

The live tests run the real chezmoi and git in the scratch setup; the fake's
tests hold `FakeChezmoi` to what the parsers make of the recorded fixtures.
"""

from __future__ import annotations

import asyncio

import pytest

from cheztui import ops
from cheztui.chezmoi import ChezmoiFailed, ChezmoiUnavailable
from cheztui.git import GitError, GitFailed
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_chezmoi import FakeChezmoi
from tests.test_ops import fixture_drift

# --- Live -------------------------------------------------------------------------


def seed(s: Scratch) -> None:
    (s.home / ".a").write_text("a\n")
    s.run("add", str(s.home / ".a"))
    s.commit_all("initial")


@requires_chezmoi
async def test_live_clean_repo_without_upstream(scratch: Scratch):
    seed(scratch)
    state = await ops.repo_state(scratch.chezmoi())
    assert state == ops.RepoState(scratch.source, "master", None, 0)


@requires_chezmoi
async def test_live_stray_file(scratch: Scratch):
    seed(scratch)
    (scratch.source / "stray").write_text("x\n")
    assert (await ops.repo_state(scratch.chezmoi())).uncommitted == 1


@requires_chezmoi
async def test_live_ahead_of_a_local_upstream(scratch: Scratch):
    # A local branch as the upstream (spec §11): the scratch repo has no remote.
    seed(scratch)
    scratch.git("branch", "upstream")
    scratch.git("branch", "--set-upstream-to=upstream")
    (scratch.source / "dot_b").write_text("b\n")
    scratch.commit_all("ahead")
    state = await ops.repo_state(scratch.chezmoi())
    assert (state.branch, state.ahead_behind, state.uncommitted) == ("master", (1, 0), 0)


@requires_chezmoi
async def test_live_detached_head(scratch: Scratch):
    seed(scratch)
    scratch.git("checkout", "-q", "--detach")
    state = await ops.repo_state(scratch.chezmoi())
    assert (state.branch, state.ahead_behind) == (None, None)


@requires_chezmoi
async def test_live_before_the_first_commit(scratch: Scratch):
    state = await ops.repo_state(scratch.chezmoi())
    assert (state.branch, state.ahead_behind, state.uncommitted) == ("master", None, 0)


# --- Errors propagate raw -----------------------------------------------------


async def test_a_failing_dump_config_propagates():
    fake = FakeChezmoi.in_sync()
    fake.fail("dump-config", b"chezmoi: bad config token=abc\n", 2)
    with pytest.raises(ChezmoiFailed) as e:
        await ops.repo_state(fake.chezmoi(), git_runner=fake.git_runner)
    assert e.value.returncode == 2
    assert "token=abc" in e.value.stderr  # unmasked: the front end masks it
    assert not [c for c in fake.log if c[0] == "git"]


@pytest.mark.parametrize("command", ["symbolic-ref", "rev-parse", "status"])
async def test_a_failing_git_read_propagates(command: str):
    fake = FakeChezmoi.in_sync()
    fake.fail(command, b"fatal: not a git repository\n", 128)
    with pytest.raises(GitFailed, match="not a git repository"):
        await ops.repo_state(fake.chezmoi(), git_runner=fake.git_runner)


async def test_repo_state_reads_the_working_tree():
    fake = FakeChezmoi.in_sync(upstream=set(), counts=(1, 2), stray=["a", "b"])
    fake.work_tree = fake.source / "tree"
    state = await ops.repo_state(fake.chezmoi(), git_runner=fake.git_runner)
    assert state == ops.RepoState(fake.source, "master", (1, 2), 2)
    git_calls = [c for c in fake.log if c[0] == "git"]
    assert git_calls
    assert all(c[1:3] == ("-C", str(fake.work_tree)) for c in git_calls)
    assert fake.log[0][-2:] == ("dump-config", "--format=json")


# --- The fake against the parsers ---------------------------------------------


async def test_drifted_fake_matches_the_recorded_fixtures():
    fake = FakeChezmoi.drifted()
    assert await ops.drift(fake.chezmoi()) == fixture_drift()


async def test_in_sync_fake_reads_only_status():
    fake = FakeChezmoi.in_sync()
    assert await ops.drift(fake.chezmoi()) == ops.Drift()
    assert fake.chezmoi_calls == [("status", "--path-style=absolute")]


async def test_fake_version_check():
    fake = FakeChezmoi.in_sync()
    assert await fake.chezmoi().check_version() == (2, 60, 0)
    fake = FakeChezmoi.in_sync(version="chezmoi version v2.59.0, commit x")
    with pytest.raises(ChezmoiUnavailable, match="too old"):
        await fake.chezmoi().check_version()
    fake = FakeChezmoi.in_sync(version=None)
    with pytest.raises(ChezmoiUnavailable, match="not found"):
        await fake.chezmoi().check_version()


async def test_fake_header_reads():
    fake = FakeChezmoi.drifted(branch=None, stray=["x"])
    state = await ops.repo_state(fake.chezmoi(), git_runner=fake.git_runner)
    assert state == ops.RepoState(fake.source, None, None, 1)


async def test_a_held_call_waits_for_its_event():
    fake = FakeChezmoi.in_sync()
    release = fake.hold("status")
    task = asyncio.ensure_future(ops.drift(fake.chezmoi()))
    await asyncio.sleep(0)
    assert fake.chezmoi_calls == [("status", "--path-style=absolute")]
    assert not task.done()
    release.set()
    assert await task == ops.Drift()


# --- The shared failure message (spec §4 Log line, §7) ----------------------------


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ChezmoiFailed(("chezmoi",), 1, "a\nchezmoi: last\n\n"), "chezmoi exited 1: chezmoi: last"),
        (ChezmoiFailed(("chezmoi",), 2, ""), "chezmoi exited 2"),
        (GitFailed(("git",), 128, "fatal: one\nfatal: two\n"), "git exited 128: fatal: two"),
        (GitFailed(("git",), 1, " \n"), "git exited 1"),
        (ChezmoiUnavailable("chezmoi not found"), "chezmoi not found"),
        (GitError("git not found"), "git not found"),
    ],
)
def test_failure_message(error, message):
    assert ops.failure_message(error) == message
