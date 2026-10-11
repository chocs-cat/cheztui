"""The scratch fixture keeps every test away from the real setup (spec §11)."""

from __future__ import annotations

import json
import os

from tests.conftest import Scratch, requires_chezmoi


def test_environment_points_under_root(scratch: Scratch):
    for var in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "GIT_CONFIG_GLOBAL",
    ):
        path = os.environ[var]
        assert path.startswith(f"{scratch.root}/"), var
    assert os.environ["HOME"] == str(scratch.home)
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"


def test_source_repo_has_no_remote(scratch: Scratch):
    assert scratch.git("remote").stdout == ""
    assert scratch.git("symbolic-ref", "--short", "HEAD").stdout.strip() == "master"


def test_config_dir_is_not_an_ancestor_of_home(scratch: Scratch):
    assert not scratch.home.is_relative_to(scratch.config.parent)


def test_config_never_sets_autopush(scratch: Scratch):
    for on in (True, False):
        scratch.set_auto_commit(on)
        assert "autopush" not in scratch.config.read_text().lower()


@requires_chezmoi
def test_dump_config_has_no_autopush(scratch: Scratch):
    for on in (True, False):
        scratch.set_auto_commit(on)
        git = json.loads(scratch.run("dump-config", "--format=json").stdout)["git"]
        assert git["autopush"] is False
        assert git["autocommit"] is on
