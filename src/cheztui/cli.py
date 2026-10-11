"""cheztui command line (spec §7).

    cheztui                     the TUI (on a terminal), else help
    cheztui status [--json]     drift rows with direction and default fix
    cheztui verify [--json]     pass/fail and the failing targets

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
from cheztui.chezmoi import Chezmoi, ChezmoiError, ChezmoiFailed

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
    except ChezmoiFailed as e:
        # The message ends with chezmoi's last error line (spec §7, DEC-25).
        _error(f"chezmoi exited {e.returncode}" + (f": {e.last_line}" if e.last_line else ""))
        return EXIT_CHEZMOI
    except ChezmoiError as e:
        _error(str(e))
        return EXIT_CHEZMOI
    return _COMMANDS[args.command](drift, args.json)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is not None:
        return asyncio.run(_read(args))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.print_help()
        return EXIT_OK
    # Imported here so `--version` and help don't pay for loading Textual.
    from cheztui.tui.app import CheztuiApp

    try:
        CheztuiApp().run()
    except KeyboardInterrupt:
        return 130
    return EXIT_OK
