"""The scratch chezmoi setup every test that runs chezmoi or git uses (spec §11).

Tests never touch the real source dir or home: `scratch` builds a HOME, a
source repo with no remote, and a chezmoi config that never enables
autoPush, all under `tmp_path`, and points the environment at them.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cheztui import watch
from cheztui.chezmoi import Chezmoi
from tests.watching import Watchers

requires_chezmoi = pytest.mark.skipif(
    shutil.which("chezmoi") is None, reason="chezmoi is not installed"
)


@dataclass
class Scratch:
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def config(self) -> Path:
        # Its own dir, never the home or above it: chezmoi refuses to add
        # anything under its config file's dir (spec §11).
        return self.root / "chezmoi" / "chezmoi.toml"

    def chezmoi(self, *, dry_run: bool = False) -> Chezmoi:
        return Chezmoi(
            config=self.config, source=self.source, destination=self.home, dry_run=dry_run
        )

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the real chezmoi against the scratch dirs, for test setup."""
        return subprocess.run(
            [
                "chezmoi",
                "--no-tty",
                "--config",
                str(self.config),
                "--source",
                str(self.source),
                "--destination",
                str(self.home),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run git in the scratch source repo."""
        return subprocess.run(
            ["git", "-C", str(self.source), *args], check=True, capture_output=True, text=True
        )

    def set_auto_commit(self, on: bool) -> None:
        # Never autoPush: the scratch repo has no remote, and a test must
        # not be one typo away from pushing somewhere real.
        self.config.write_text(f"[git]\n    autoCommit = {'true' if on else 'false'}\n")

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scratch:
    # chezmoi reports realpaths, and macOS's tmp_path sits behind /var → /private/var.
    s = Scratch(tmp_path.resolve())
    s.home.mkdir()
    (s.home / ".config").mkdir()
    xdg = s.root / "xdg"
    for name in ("data", "cache", "state"):
        (xdg / name).mkdir(parents=True)

    gitconfig = s.root / "gitconfig"
    gitconfig.write_text(
        "[user]\n"
        "    name = cheztui tests\n"
        "    email = tests@example.invalid\n"
        "[init]\n"
        "    defaultBranch = master\n"
    )
    monkeypatch.setenv("HOME", str(s.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(s.home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg / "state"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    s.config.parent.mkdir()
    s.set_auto_commit(False)
    subprocess.run(["git", "init", "-q", "-b", "master", str(s.source)], check=True)
    return s


@pytest.fixture(autouse=True)
def watchers(monkeypatch: pytest.MonkeyPatch) -> Watchers:
    """The app's default watcher factory, made fake: a TUI test watches no real
    dir unless it sets `app.watcher_factory` to the real `Watcher` (spec §5
    *Watching in the TUI*)."""
    factory = Watchers()
    monkeypatch.setattr(watch, "Watcher", factory)
    return factory
