"""The command line and its TUI launch (spec §7; DEC-19, DEC-21, DEC-25, DEC-33).

Every command that reaches chezmoi passes the scratch `--chezmoi-config`,
`--source` and `--destination`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheztui import __version__, cli
from cheztui.cli import main
from cheztui.tui.app import CheztuiApp
from tests.conftest import Scratch, requires_chezmoi
from tests.test_chezmoi_reads import build_drift


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"cheztui {__version__}\n"


# --- No arguments: the TUI (spec §7) ---------------------------------------------------


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[CheztuiApp]:
    """A terminal, a scratch config dir, and `run` capturing the app instead of running it."""
    monkeypatch.setattr(cli, "_on_terminal", lambda: True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    apps: list[CheztuiApp] = []
    monkeypatch.setattr(CheztuiApp, "run", lambda self: apps.append(self))
    return apps


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "xdg" / "cheztui" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_no_arguments_runs_the_app_with_the_global_flags(terminal, tmp_path, capsys):
    write_config(tmp_path, '[ui]\ntheme = "nord"\n')
    argv = ["-n", "--chezmoi-config", "/c.toml", "--source", "/src", "--destination", "/dst"]
    assert main(argv) == 0
    [app] = terminal
    chezmoi = app.chezmoi
    assert (chezmoi.config, chezmoi.source, chezmoi.destination, chezmoi.dry_run) == (
        Path("/c.toml"),
        Path("/src"),
        Path("/dst"),
        True,
    )
    assert app.settings.ui.theme == "nord"
    assert capsys.readouterr() == ("", "")


def test_no_arguments_without_flags(terminal):
    assert main([]) == 0
    [app] = terminal
    assert (app.chezmoi.config, app.chezmoi.source, app.chezmoi.dry_run) == (None, None, False)


def test_a_config_error_never_starts_the_app(terminal, tmp_path, capsys, monkeypatch):
    path = write_config(tmp_path, "[invalid")
    built: list[object] = []
    monkeypatch.setattr(CheztuiApp, "__init__", lambda self, *a, **k: built.append(a))
    assert main([]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"cheztui: {path}: not valid TOML")
    assert built == []


def test_an_unknown_theme_exits_2(terminal, tmp_path, capsys):
    path = write_config(tmp_path, '[ui]\ntheme = "no-such-theme"\n')
    assert main([]) == 2
    assert capsys.readouterr() == (
        "",
        f"cheztui: {path}: ui.theme 'no-such-theme' is not a Textual theme\n",
    )
    assert terminal == []


def test_a_bad_global_flag_exits_2(terminal, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-flag"])
    assert exc.value.code == 2
    assert terminal == []


def test_without_a_terminal_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_on_terminal", lambda: False)
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: cheztui")


def flags(s: Scratch) -> list[str]:
    return [
        "--chezmoi-config",
        str(s.config),
        "--source",
        str(s.source),
        "--destination",
        str(s.home),
    ]


def run(s: Scratch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main([*flags(s), *args])
    out, err = capsys.readouterr()
    return code, out, err


# --- status and verify ------------------------------------------------------------


@requires_chezmoi
def test_status_json(scratch: Scratch, capsys):
    build_drift(scratch)
    code, out, err = run(scratch, capsys, "status", "--json")
    assert (code, err) == (0, "")
    h, src = scratch.home, scratch.source

    def row(name, source, direction, fix, columns, tags=()):
        return {
            "target": str(h / name),
            "source": str(src / source),
            "direction": direction,
            "default_fix": fix,
            "columns": columns,
            "tags": list(tags),
        }

    assert json.loads(out) == {
        "rows": [
            row(".both file", "dot_both file", "both", "merge", "MM"),
            row(".gone", "dot_gone", "you", "apply", "DA"),
            row(".mine", "dot_mine", "you", "re-add", "MM"),
            row(".tool", "executable_dot_tool", "src", "apply", " M", ["exec"]),
            row("hello.sh", "run_onchange_hello.sh", "run", "apply", " R"),
        ]
    }


@requires_chezmoi
def test_status_json_source_is_null_without_a_source(scratch: Scratch, capsys):
    (scratch.home / ".rm").write_text("rm\n")
    scratch.run("add", str(scratch.home / ".rm"))
    (scratch.source / "dot_rm").unlink()
    (scratch.source / ".chezmoiremove").write_text(".rm\n")
    with (scratch.home / ".rm").open("a") as f:
        f.write("edited\n")
    code, out, _ = run(scratch, capsys, "status", "--json")
    assert code == 0
    [row] = json.loads(out)["rows"]
    assert (row["source"], row["columns"], row["default_fix"]) == (None, "MD", "apply")


@requires_chezmoi
def test_status_human(scratch: Scratch, capsys):
    build_drift(scratch)
    code, out, err = run(scratch, capsys, "status")
    assert (code, err) == (0, "")
    assert out == (
        "both  MM  ~/.both file  merge\n"
        "you→  DA  ~/.gone       apply\n"
        "you→  MM  ~/.mine       re-add\n"
        "src→   M  ~/.tool       apply   exec\n"
        "run    R  ~/hello.sh    apply\n"
    )


@requires_chezmoi
def test_verify_json_and_human_on_drift(scratch: Scratch, capsys):
    build_drift(scratch)
    code, out, _ = run(scratch, capsys, "verify", "--json")
    h = scratch.home
    assert code == 1
    assert json.loads(out) == {
        "verified": False,
        "failing": [str(h / n) for n in (".both file", ".gone", ".mine", ".tool", "hello.sh")],
    }
    code, out, _ = run(scratch, capsys, "verify")
    assert code == 1
    assert out == ("✗ verify\n  ~/.both file\n  ~/.gone\n  ~/.mine\n  ~/.tool\n  ~/hello.sh\n")


@requires_chezmoi
def test_verify_lists_a_pending_script(scratch: Scratch, capsys):
    (scratch.source / "run_once_setup.sh").write_text("#!/bin/sh\ntrue\n")
    code, out, _ = run(scratch, capsys, "verify")
    assert (code, out) == (1, "✗ verify\n  ~/setup.sh\n")


@requires_chezmoi
def test_clean_setup(scratch: Scratch, capsys):
    (scratch.home / ".x").write_text("x\n")
    scratch.run("add", str(scratch.home / ".x"))
    assert run(scratch, capsys, "status") == (0, "✓ in sync\n", "")
    assert run(scratch, capsys, "verify") == (0, "✓ verify\n", "")
    code, out, _ = run(scratch, capsys, "status", "--json")
    assert (code, json.loads(out)) == (0, {"rows": []})
    code, out, _ = run(scratch, capsys, "verify", "--json")
    assert (code, json.loads(out)) == (0, {"verified": True, "failing": []})


@requires_chezmoi
def test_dry_run_flag_reads(scratch: Scratch, capsys):
    build_drift(scratch)
    code, out, _ = run(scratch, capsys, "status", "--json")
    assert run(scratch, capsys, "-n", "status", "--json") == (0, out, "")
    assert code == 0


# --- Errors -----------------------------------------------------------------------


def test_config_error_exits_2(scratch: Scratch, capsys):
    path = scratch.home / ".config" / "cheztui" / "config.toml"
    path.parent.mkdir()
    path.write_text("[ui]\ndiff_context = -1\n")
    code, out, err = run(scratch, capsys, "status", "--json")
    assert (code, out) == (2, "")
    assert err.startswith(f"cheztui: {path}: ")
    assert err.count("\n") == 1


def stub_chezmoi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "chezmoi"
    stub.write_text(f"#!/bin/sh\n{script}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}")


@pytest.mark.parametrize("command", ["status", "verify"])
@pytest.mark.parametrize("as_json", [False, True])
def test_old_chezmoi_exits_3(scratch: Scratch, capsys, monkeypatch, command, as_json):
    stub_chezmoi(scratch.root, monkeypatch, 'echo "chezmoi version v2.59.0"')
    code, out, err = run(scratch, capsys, command, *(["--json"] if as_json else []))
    assert (code, out) == (3, "")
    assert err == "cheztui: chezmoi 2.59.0 is too old: cheztui needs 2.60.0 or later\n"


def test_missing_chezmoi_exits_3(scratch: Scratch, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(scratch.root / "nowhere"))
    code, out, err = run(scratch, capsys, "status")
    assert (code, out, err) == (3, "", "cheztui: chezmoi not found\n")


@requires_chezmoi
@pytest.mark.parametrize("command", ["status", "verify"])
def test_failed_read_exits_3_with_chezmoi_last_line(scratch: Scratch, capsys, command):
    (scratch.source / "dot_bad.tmpl").write_text("{{ .nope.x }}\n")
    code, out, err = run(scratch, capsys, command, "--json")
    assert (code, out) == (3, "")
    assert err.startswith("cheztui: chezmoi exited 1: ")
    assert "nope" in err
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    "args",
    [
        ["status", "--source", "/x"],
        ["status", "-n"],
        ["verify", "--chezmoi-config", "/x"],
        ["status", "--destination", "/x"],
    ],
)
def test_global_flags_after_the_command_are_a_usage_error(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_help_lists_only_the_built_commands(capsys):
    # The command list grows only as commands ship (spec §7, §12).
    with pytest.raises(SystemExit):
        main(["--help"])
    lines = capsys.readouterr().out.splitlines()
    commands = [
        line.split()[0] for line in lines if line[:5].isspace() is False and line[:4] == "    "
    ]
    assert commands == ["status", "verify", "edit", "add"]
