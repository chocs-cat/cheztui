"""Source-repo queries against real git in the scratch repo (spec §5, §10; DEC-19)."""

from __future__ import annotations

import pytest

from cheztui.git import Commit, Git, GitError, GitFailed
from cheztui.runner import run_subprocess
from tests.conftest import Scratch


@pytest.fixture
def git(scratch: Scratch) -> Git:
    return Git(work_tree=scratch.source)


def sha(scratch: Scratch, rev: str = "HEAD") -> str:
    return scratch.git("rev-parse", rev).stdout.strip()


async def test_porcelain(scratch: Scratch, git: Git):
    assert await git.porcelain() == []
    (scratch.source / "kept").write_text("1\n")
    (scratch.source / "old name").write_text("moved\n")
    scratch.commit_all("initial")
    assert await git.porcelain() == []

    (scratch.source / "kept").write_text("2\n")
    (scratch.source / "sub" / "deeper").mkdir(parents=True)
    (scratch.source / "sub" / "deeper" / "new file").write_text("x\n")
    scratch.git("mv", "old name", "new name")
    assert sorted(await git.porcelain()) == ["kept", "new name", "sub/deeper/new file"]


async def test_porcelain_paths_are_not_quoted(scratch: Scratch, git: Git):
    (scratch.source / 'tab\there "q"').write_text("x\n")
    assert await git.porcelain() == ['tab\there "q"']


async def test_head(scratch: Scratch, git: Git):
    assert await git.head() is None
    (scratch.source / "f").write_text("x\n")
    scratch.commit_all("first: with a colon")
    assert await git.head() == Commit(sha(scratch), "first: with a colon")


async def test_branch(scratch: Scratch, git: Git):
    assert await git.branch() == "master"  # unborn, still on master
    (scratch.source / "f").write_text("x\n")
    scratch.commit_all("first")
    assert await git.branch() == "master"
    scratch.git("checkout", "-q", "--detach")
    assert await git.branch() is None


async def test_no_upstream(scratch: Scratch, git: Git):
    assert await git.ahead_behind() is None  # no HEAD either
    (scratch.source / "f").write_text("x\n")
    scratch.commit_all("first")
    assert await git.ahead_behind() is None
    assert await git.upstream_contains(sha(scratch)) is False


async def test_local_branch_as_upstream(scratch: Scratch, git: Git):
    # spec §11: a test of push state sets a local branch as the upstream.
    (scratch.source / "f").write_text("1\n")
    scratch.commit_all("first")
    scratch.git("branch", "base")
    scratch.git("branch", "--set-upstream-to=base")
    assert await git.ahead_behind() == (0, 0)

    (scratch.source / "f").write_text("2\n")
    scratch.commit_all("second")
    head = sha(scratch)
    assert await git.ahead_behind() == (1, 0)
    assert await git.upstream_contains(head) is False

    scratch.git("branch", "-f", "base", "HEAD")
    assert await git.ahead_behind() == (0, 0)
    assert await git.upstream_contains(head) is True

    scratch.git("reset", "-q", "--hard", "HEAD~1")
    assert await git.ahead_behind() == (0, 1)


async def test_upstream_contains_unknown_commit_raises(scratch: Scratch, git: Git):
    (scratch.source / "f").write_text("1\n")
    scratch.commit_all("first")
    scratch.git("branch", "base")
    scratch.git("branch", "--set-upstream-to=base")
    with pytest.raises(GitFailed) as exc:
        await git.upstream_contains("0" * 40)
    assert exc.value.returncode not in (0, 1)
    assert exc.value.argv[:3] == ("git", "-C", str(scratch.source))


async def test_reads_never_take_optional_locks(scratch: Scratch):
    seen = []

    async def runner(argv, *, cwd=None, env=None, stdin=None):
        seen.append(env)
        return await run_subprocess(argv, cwd=cwd, env=env, stdin=stdin)

    await Git(runner, work_tree=scratch.source).porcelain()
    assert seen == [{"GIT_OPTIONAL_LOCKS": "0"}]


async def test_missing_binary(scratch: Scratch):
    with pytest.raises(GitError):
        await Git(binary="cheztui-no-such-git", work_tree=scratch.source).head()
