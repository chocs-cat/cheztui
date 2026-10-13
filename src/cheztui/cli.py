"""cheztui command line (spec §7).

    cheztui                     the TUI (on a terminal), else help
    cheztui status [--json]     drift rows with direction and default fix
    cheztui verify [--json]     pass/fail and the failing targets
    cheztui edit TARGET --ops FILE|- [--dry-run] [--allow-drift] [--reveal] [--json]
    cheztui add PATH [--template] [--encrypt] [--dry-run] [--reveal] [--json]

Global flags go before the command: -n/--dry-run, --chezmoi-config PATH,
--source PATH, --destination PATH (DEC-19).

Exit codes: 0 ok, 1 some target failed or was refused (for verify: not
verified), 2 usage/config error, 3 chezmoi missing or broken, or a chezmoi
read failed (DEC-25).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from cheztui import __version__, config, ops
from cheztui.chezmoi import Chezmoi, ChezmoiError
from cheztui.edit import SpecError, parse_spec

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CHEZMOI = 0, 1, 2, 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cheztui",
        description="A clean, fast TUI for chezmoi. With no command on a terminal, opens the TUI.",
    )
    p.add_argument("--version", action="version", version=f"cheztui {__version__}")
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="pass --dry-run to every chezmoi call"
    )
    p.add_argument("--chezmoi-config", type=Path, metavar="PATH", help="chezmoi's config file")
    p.add_argument("--source", type=Path, metavar="PATH", help="chezmoi's source directory")
    p.add_argument("--destination", type=Path, metavar="PATH", help="chezmoi's destination")
    commands = p.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_text in (
        ("status", "drift rows with direction and default fix"),
        ("verify", "whether every target matches its target state"),
    ):
        sub = commands.add_parser(name, help=help_text, description=help_text.capitalize() + ".")
        sub.add_argument("--json", action="store_true", help="print JSON on stdout")
    edit = commands.add_parser("edit", help="scripted edit of one managed file")
    edit.add_argument("target", type=Path)
    edit.add_argument("--ops", required=True, metavar="FILE|-", help="JSON ops spec, or stdin")
    edit.add_argument("--dry-run", dest="command_dry_run", action="store_true")
    edit.add_argument("--allow-drift", action="store_true")
    edit.add_argument("--reveal", action="store_true")
    edit.add_argument("--json", action="store_true", help="print JSON on stdout")
    add = commands.add_parser("add", help="bring a new file or directory under management")
    add.add_argument("target", type=Path, metavar="path")
    add.add_argument("--template", action="store_true")
    add.add_argument("--encrypt", action="store_true")
    add.add_argument("--dry-run", dest="command_dry_run", action="store_true")
    add.add_argument("--reveal", action="store_true")
    add.add_argument("--json", action="store_true", help="print JSON on stdout")
    return p


def _error(message: str) -> None:
    print(f"cheztui: {message}", file=sys.stderr)


def _display(target: Path, home: Path) -> str:
    """A target under $HOME as `~/…` (spec §7)."""
    try:
        relative = target.relative_to(home)
    except ValueError:
        return str(target)
    return "~" if relative == Path() else f"~/{relative}"


def _table(lines: Sequence[Sequence[str]]) -> list[str]:
    """Columns two spaces apart, padded to the widest value, trailing spaces stripped."""
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    return [
        "  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)).rstrip() for line in lines
    ]


def _status(drift: ops.Drift, as_json: bool) -> int:
    if as_json:
        rows = [
            {
                "target": str(r.target),
                "source": str(r.source) if r.source is not None else None,
                "direction": r.direction,
                "default_fix": r.default_fix,
                "columns": r.columns,
                "tags": list(r.tags),
            }
            for r in drift.rows
        ]
        print(json.dumps({"rows": rows}, indent=2))
    elif not drift.rows:
        print("✓ in sync")
    else:
        home = Path.home()
        lines = [
            (
                ops.DIRECTION_LABELS[r.direction],
                r.columns,
                _display(r.target, home),
                r.default_fix,
                " ".join(r.tags),
            )
            for r in drift.rows
        ]
        print("\n".join(_table(lines)))
    return EXIT_OK


def _verify(drift: ops.Drift, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"verified": drift.verified, "failing": [str(t) for t in drift.failing]},
                indent=2,
            )
        )
    elif drift.verified:
        print("✓ verify")
    else:
        home = Path.home()
        print("\n".join(["✗ verify", *(f"  {_display(t, home)}" for t in drift.failing)]))
    return EXIT_OK if drift.verified else EXIT_FAILED


_COMMANDS: dict[str, Callable[[ops.Drift, bool], int]] = {"status": _status, "verify": _verify}


async def _read(args: argparse.Namespace) -> int:
    """Load the config, check chezmoi, read the Drift model, print it (spec §7)."""
    try:
        config.load()
    except config.ConfigError as e:
        _error(str(e))
        return EXIT_USAGE
    chezmoi = Chezmoi(
        config=args.chezmoi_config,
        source=args.source,
        destination=args.destination,
        dry_run=args.dry_run,
    )
    try:
        await chezmoi.check_version()
        drift = await ops.drift(chezmoi)
    except ChezmoiError as e:
        # The message ends with chezmoi's last error line (spec §7, DEC-25).
        _error(ops.failure_message(e))
        return EXIT_CHEZMOI
    return _COMMANDS[args.command](drift, args.json)


def _refusal(result: ops.Refusal, as_json: bool) -> int:
    if as_json:
        payload: dict[str, object] = {"error": result.message}
        if result.stray:
            payload["stray"] = list(result.stray)
        print(json.dumps(payload, indent=2))
    else:
        _error(result.message)
        for path in result.stray:
            print(f"  {path}", file=sys.stderr)
    return EXIT_FAILED


def _script_output(result: ops.ScriptResult | ops.Refusal, as_json: bool, action: str) -> int:
    if isinstance(result, ops.Refusal):
        return _refusal(result, as_json)
    name = _display(result.target, Path.home())
    if result.template and action == "edit":
        print(
            f"note: {name} is a template; the applied result differs from the edit", file=sys.stderr
        )
    if result.warning:
        print("warning: uncommitted files in the source tree:", file=sys.stderr)
        for path in result.warning:
            print(f"  {path}", file=sys.stderr)
    if as_json:
        payload: dict[str, object] = {
            "target": str(result.target),
            "source": str(result.source) if result.source is not None else None,
            "template": result.template,
            "encrypted": result.encrypted,
            "diff": result.diff,
            "commit": {"sha": result.commit.sha, "subject": result.commit.subject}
            if result.commit is not None
            else None,
            "pushed": result.pushed,
            "verified": result.verified,
        }
        if action == "edit":
            payload["skipped"] = result.skipped
        if result.error is not None:
            payload["error"] = result.error
        print(json.dumps(payload, indent=2))
    else:
        if result.diff:
            print(result.diff, end="")
        if result.error is not None:
            if result.stderr:
                print(
                    result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n"
                )
            _error(result.error)
        elif result.skipped:
            print(f"skipped {name}: already contains {result.skip_text!r}, leaving unchanged")
        else:
            # The TUI's log-line form, from the same code (§8 Output, DEC-35).
            print(
                ops.outcome_line(
                    "edit" if action == "edit" else "add",
                    (result.target,),
                    dry_run=result.dry_run,
                    commit=result.commit,
                    pushed=result.pushed,
                    state=result.state,
                )
            )
    return EXIT_FAILED if result.error is not None or result.verified is False else EXIT_OK


async def _write(args: argparse.Namespace) -> int:
    # Read and parse the spec before config, version checks or any chezmoi call (§8).
    spec = None
    if args.command == "edit":
        try:
            raw = (
                sys.stdin.read() if args.ops == "-" else Path(args.ops).read_bytes().decode("utf-8")
            )
            spec = parse_spec(raw)
        except (OSError, UnicodeError, SpecError) as e:
            return _refusal(ops.Refusal(str(e)), args.json)
    try:
        settings = config.load()
    except config.ConfigError as e:
        _error(str(e))
        return EXIT_USAGE
    chezmoi = Chezmoi(
        config=args.chezmoi_config,
        source=args.source,
        destination=args.destination,
        dry_run=args.dry_run,
    )
    try:
        await chezmoi.check_version()
        writes = ops.WriteOps(chezmoi, settings)
        if args.command == "edit":
            assert spec is not None
            result = await writes.edit(
                args.target,
                spec,
                dry_run=args.command_dry_run,
                allow_drift=args.allow_drift,
                reveal=args.reveal,
            )
        else:
            result = await writes.add(
                args.target,
                template=args.template,
                encrypt=args.encrypt,
                dry_run=args.command_dry_run,
                reveal=args.reveal,
            )
    except ChezmoiError as e:
        _error(ops.failure_message(e))
        return EXIT_CHEZMOI
    return _script_output(result, args.json, args.command)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_editor":
        from cheztui.edit import editor_main

        return editor_main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ("edit", "add"):
        return asyncio.run(_write(args))
    if args.command is not None:
        return asyncio.run(_read(args))
    if not _on_terminal():
        parser.print_help()
        return EXIT_OK
    return _tui(args)


def _on_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _tui(args: argparse.Namespace) -> int:
    """Load the config, check its theme, and run the TUI (spec §7, §9; DEC-33)."""
    try:
        settings = config.load()
    except config.ConfigError as e:
        _error(str(e))
        return EXIT_USAGE
    # Imported here so `--version` and help don't pay for loading Textual.
    from cheztui.tui.app import THEMES, CheztuiApp

    if settings.ui.theme not in THEMES:
        _error(f"{settings.path}: ui.theme '{settings.ui.theme}' is not a Textual theme")
        return EXIT_USAGE
    chezmoi = Chezmoi(
        config=args.chezmoi_config,
        source=args.source,
        destination=args.destination,
        dry_run=args.dry_run,
    )
    try:
        CheztuiApp(settings, chezmoi).run()
    except KeyboardInterrupt:
        return 130
    return EXIT_OK
