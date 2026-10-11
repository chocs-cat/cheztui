"""The chezmoi wrapper: argv, errors and the version check (spec §10, DEC-21)."""

from __future__ import annotations

import itertools
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from cheztui.chezmoi import Chezmoi, ChezmoiFailed, ChezmoiUnavailable
from cheztui.runner import Completed
from tests.conftest import Scratch, requires_chezmoi


class FakeRunner:
    """Records each argv and answers with canned output."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, stderr: bytes = b"") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> Completed:
        self.calls.append(tuple(argv))
        return Completed(tuple(argv), self.returncode, self.stdout, self.stderr)


@pytest.mark.parametrize(
    ("config", "source", "destination", "dry_run"),
    list(
        itertools.product(
            [None, Path("/c/chezmoi.toml")], [None, Path("/s")], [None, Path("/d")], [False, True]
        )
    ),
)
async def test_run_builds_argv_in_order(config, source, destination, dry_run):
    runner = FakeRunner()
    cz = Chezmoi(runner, config=config, source=source, destination=destination, dry_run=dry_run)
    await cz.run("status", "--path-style=absolute")
    expected = ["chezmoi", "--no-tty"]
    if config:
        expected += ["--config", "/c/chezmoi.toml"]
    if source:
        expected += ["--source", "/s"]
    if destination:
        expected += ["--destination", "/d"]
    if dry_run:
        expected += ["--dry-run"]
    expected += ["status", "--path-style=absolute"]
    assert runner.calls == [tuple(expected)]


async def test_run_uses_the_binary():
    runner = FakeRunner()
    await Chezmoi(runner, binary="/opt/chezmoi").run("managed")
    assert runner.calls == [("/opt/chezmoi", "--no-tty", "managed")]


async def test_nonzero_exit_raises_failed_with_last_line():
    runner = FakeRunner(returncode=1, stderr=b"warning: x\nchezmoi: boom\n\n")
    with pytest.raises(ChezmoiFailed) as exc:
        await Chezmoi(runner).run("apply")
    assert exc.value.argv == ("chezmoi", "--no-tty", "apply")
    assert exc.value.returncode == 1
    assert exc.value.stderr == "warning: x\nchezmoi: boom\n\n"
    assert exc.value.last_line == "chezmoi: boom"
    assert str(exc.value) == "chezmoi: boom"


async def test_nonzero_exit_without_check_returns():
    runner = FakeRunner(returncode=1)
    done = await Chezmoi(runner).run("verify", check=False)
    assert done.returncode == 1


async def test_missing_binary_is_unavailable():
    cz = Chezmoi(binary="cheztui-no-such-chezmoi")
    with pytest.raises(ChezmoiUnavailable):
        await cz.run("status")
    with pytest.raises(ChezmoiUnavailable):
        await cz.check_version()


@pytest.mark.parametrize(
    ("output", "version"),
    [
        (b"chezmoi version v2.60.0, commit abc, built at x, built by y\n", (2, 60, 0)),
        (b"chezmoi version v2.71.0, commit abc\n", (2, 71, 0)),
        (b"chezmoi version 3.0.1\n", (3, 0, 1)),
        (b"chezmoi version dev, commit abc, built at x\n", None),
    ],
)
async def test_version_passes(output, version):
    runner = FakeRunner(stdout=output)
    cz = Chezmoi(runner)
    assert await cz.check_version() == version
    assert runner.calls == [("chezmoi", "--version")]


async def test_version_too_old_raises():
    runner = FakeRunner(stdout=b"chezmoi version v2.59.3, commit abc\n")
    with pytest.raises(ChezmoiUnavailable, match=r"2\.59\.3.*2\.60\.0"):
        await Chezmoi(runner).check_version()


async def test_version_runs_once():
    runner = FakeRunner(stdout=b"chezmoi version v2.60.0\n")
    cz = Chezmoi(runner)
    await cz.check_version()
    await cz.check_version()
    assert len(runner.calls) == 1


async def test_version_too_old_raises_every_time_but_runs_once():
    runner = FakeRunner(stdout=b"chezmoi version v2.1.0\n")
    cz = Chezmoi(runner)
    for _ in range(2):
        with pytest.raises(ChezmoiUnavailable):
            await cz.check_version()
    assert len(runner.calls) == 1


@requires_chezmoi
async def test_live_version_passes(scratch: Scratch):
    version = await scratch.chezmoi().check_version()
    assert version is None or version >= (2, 60, 0)


def test_go_quote():
    from cheztui.chezmoi import go_quote

    assert go_quote(Path('/s/a "b" \\ c')) == '"/s/a \\"b\\" \\\\ c"'
    assert go_quote(Path("/s/café\n")) == '"/s/caf\\xc3\\xa9\\x0a"'
    assert go_quote(Path(os.fsdecode(b"/s/\xff"))) == '"/s/\\xff"'


async def test_source_text_includes_the_quoted_path():
    runner = FakeRunner(stdout=b"{{ literal }}\n\xff")
    text = await Chezmoi(runner).source_text(Path('/s/a "b"'))
    assert runner.calls == [
        ("chezmoi", "--no-tty", "execute-template", '{{ include "/s/a \\"b\\"" }}')
    ]
    assert text == "{{ literal }}\n�"


async def test_run_with_an_injected_runner_and_env():
    default, injected = FakeRunner(), FakeRunner(returncode=1)
    envs = []

    async def recording(argv, *, cwd=None, env=None, stdin=None):
        envs.append(env)
        return await injected(argv)

    cz = Chezmoi(default, source=Path("/s"))
    done = await cz.run("merge", "/h/a", check=False, runner=recording, env={"EDITOR": "x"})
    assert done.returncode == 1
    assert default.calls == []
    assert injected.calls == [("chezmoi", "--no-tty", "--source", "/s", "merge", "/h/a")]
    assert envs == [{"EDITOR": "x"}]
