"""The default subprocess runner (spec §10, DEC-6)."""

import asyncio
import os
from pathlib import Path

import pytest

from cheztui.runner import run_subprocess


async def test_returns_exit_code_and_output():
    done = await run_subprocess(["sh", "-c", "printf out; printf err >&2; exit 3"])
    assert done.argv == ("sh", "-c", "printf out; printf err >&2; exit 3")
    assert (done.returncode, done.stdout, done.stderr) == (3, b"out", b"err")


async def test_env_merges_over_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CHEZTUI_TEST_KEPT", "kept")
    done = await run_subprocess(
        ["sh", "-c", 'printf "%s %s" "$CHEZTUI_TEST_KEPT" "$CHEZTUI_TEST_ADDED"; pwd'],
        env={"CHEZTUI_TEST_ADDED": "added"},
        cwd=tmp_path,
    )
    lines = done.stdout.decode().split("\n")
    assert lines[0].startswith("kept added")
    assert Path(lines[0].removeprefix("kept added")).samefile(tmp_path)


async def test_stdin_is_passed():
    done = await run_subprocess(["cat"], stdin=b"hello")
    assert done.stdout == b"hello"


async def test_missing_binary_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        await run_subprocess(["cheztui-no-such-binary"])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelling_kills_the_child(tmp_path):
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(
        run_subprocess(["sh", "-c", f'echo $$ > "{pidfile}"; exec sleep 30'])
    )
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    pid = int(pidfile.read_text())
    assert _alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # run_subprocess waited for the child, so it is reaped, not a zombie.
    assert not _alive(pid)
