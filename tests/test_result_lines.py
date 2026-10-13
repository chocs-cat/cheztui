"""Result lines and session-log entries in `ops.py` (spec §6 Drift *Result*, §8 Output; DEC-35).

The lines are pure functions of `CommandResult`s and `WriteResult`s, built
here by hand; the copy check runs the CLI's printer over `ScriptResult`s.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cheztui import cli, ops
from cheztui.git import Commit
from cheztui.ops import CommandResult, ScriptResult, WriteCommand, WriteEntry, WriteResult

HOME = Path("/scratch/home")
X, Y, Z = HOME / ".x", HOME / ".y", HOME / ".z"
COMMIT = Commit("a1b2c3d" + "e" * 33, "Update dot_x")


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(HOME))


def command(action: ops.Action, *targets: Path) -> WriteCommand:
    return WriteCommand(action, targets, ("chezmoi", action, *map(str, targets)), False)


def done(action: ops.Action, *targets: Path, **kwargs) -> CommandResult:
    kwargs.setdefault("outcome", "ok")
    return CommandResult(command(action, *(targets or (X,))), **kwargs)


# --- One command's line (spec §6 *Result*'s table) ------------------------------------

VERB = [
    ("apply", "applied"),
    ("re-add", "re-added"),
    ("forget", "forgot"),
    ("edit", "edited"),
    ("merge", "merged"),
    ("add", "added"),
]


@pytest.mark.parametrize(("action", "verb"), VERB)
@pytest.mark.parametrize(("targets", "name"), [((X,), "~/.x"), ((X, Y, Z), "3 targets")])
@pytest.mark.parametrize(("pushed", "push"), [(True, "pushed"), (False, "not pushed")])
def test_committed(action, verb, targets, name, pushed, push):
    result = done(action, *targets, commit=COMMIT, pushed=pushed, state="committed")
    assert ops.command_line(result) == f'{verb} {name} → a1b2c3d "Update dot_x" ({push})'


@pytest.mark.parametrize(("action", "verb"), VERB)
@pytest.mark.parametrize("state", ["not committed", "no commit"])
def test_ran_without_a_commit(action, verb, state):
    assert ops.command_line(done(action, X, Y, state=state)) == f"{verb} 2 targets ({state})"


@pytest.mark.parametrize(("action", "_"), VERB)
def test_dry_run(action, _):
    result = done(action, outcome="dry run", state="dry run")
    assert ops.command_line(result) == f"would {action} ~/.x (dry run)"


def test_an_interactive_dry_run():
    # What execute returns for `m` under -n (DEC-29).
    merge = WriteCommand("merge", (X,), ("chezmoi", "merge", str(X)), True)
    result = CommandResult(merge, "dry run", state="dry run")
    assert ops.command_line(result) == "would merge ~/.x (dry run)"


def test_failed_refused_and_unreadable():
    failed = done("apply", X, Y, outcome="failed", returncode=1, error="chezmoi: boom")
    assert ops.command_line(failed) == "✗ apply 2 targets failed: chezmoi: boom"
    refused = done("re-add", outcome="refused", error="the source tree has uncommitted changes")
    assert ops.command_line(refused) == (
        "✗ re-add ~/.x refused: the source tree has uncommitted changes"
    )
    unreadable = done("apply", reporting_error="git exited 128: fatal: bad object")
    assert ops.command_line(unreadable) == (
        "✗ apply ~/.x ran, but reading the result failed: git exited 128: fatal: bad object"
    )


def test_a_target_outside_home_is_absolute():
    assert ops.command_line(done("apply", Path("/etc/x"), state="no commit")) == (
        "applied /etc/x (no commit)"
    )


# --- The CLI prints the same lines (spec §8 Output; DEC-35) ---------------------------


@pytest.mark.parametrize("action", ["edit", "add"])
@pytest.mark.parametrize(
    "fields",
    [
        {"commit": COMMIT, "pushed": True, "state": "committed"},
        {"commit": COMMIT, "pushed": False, "state": "committed"},
        {"state": "not committed"},
        {"state": "no commit"},
        {"dry_run": True},
    ],
)
def test_the_cli_prints_the_shared_line(capsys, action, fields):
    result = ScriptResult(X, Path("/scratch/source/dot_x"), False, False, **fields)
    assert cli._script_output(result, False, action) == 0
    expected = ops.outcome_line(
        action,
        (X,),
        dry_run=result.dry_run,
        commit=result.commit,
        pushed=result.pushed,
        state=result.state,
    )
    assert capsys.readouterr().out == f"{expected}\n"


# --- A WriteResult's entries and log line (spec §6 *Result*) ---------------------------


def test_a_batch_whose_second_command_failed():
    first = done("apply", X, state="no commit")
    second = done(
        "re-add",
        Y,
        Z,
        outcome="failed",
        returncode=1,
        stderr="chezmoi: one\nchezmoi: two\n",
        error="chezmoi: two",
    )
    merge = command("merge", X)
    shown = ops.report(WriteResult(results=(first, second), unrun=(merge,), refresh=True))
    assert shown.line == "applied ~/.x (no commit); ✗ re-add 2 targets failed: chezmoi: two"
    assert shown.failed
    assert shown.entries == (
        ("applied ~/.x (no commit)", None),
        ("✗ re-add 2 targets failed: chezmoi: two", "chezmoi: one\nchezmoi: two"),
        ("not run: merge ~/.x", None),
    )


def test_a_failure_with_no_stderr_logs_its_error():
    result = done("apply", outcome="failed", error="chezmoi missing")
    assert ops.report(WriteResult(results=(result,))).entries == (
        ("✗ apply ~/.x failed: chezmoi missing", "chezmoi missing"),
    )


def test_a_refusal_at_the_first_recheck():
    refused = done(
        "apply",
        X,
        outcome="refused",
        error="the source tree has uncommitted changes",
        stray=("dot_a", "dot_b"),
    )
    shown = ops.report(WriteResult(results=(refused,), unrun=(command("re-add", Y),)))
    assert shown.line == "✗ apply ~/.x refused: the source tree has uncommitted changes"
    assert shown.failed
    assert shown.entries == (
        (shown.line, "dot_a\ndot_b"),
        ("not run: re-add ~/.y", None),
    )


def test_a_refused_plan():
    shown = ops.report(WriteResult(refused="nothing is selected"))
    assert (shown.line, shown.failed, shown.entries) == (
        "✗ nothing is selected",
        True,
        (("✗ nothing is selected", None),),
    )


def test_a_clean_result_isnt_failed():
    shown = ops.report(WriteResult(results=(done("apply", state="no commit"),), refresh=True))
    assert (shown.line, shown.failed) == ("applied ~/.x (no commit)", False)


def test_left_out_of_an_interactive_plan():
    entry = WriteEntry(X, None, "merge", False, excluded="a directory")
    assert ops.left_out_line(entry) == "left out of merge: ~/.x (a directory)"
