"""cheztui's own config file, loaded and saved with tomlkit (spec §9).

A missing file means the defaults. An unknown key or table is kept and
ignored. Saving keeps the file's comments and order, and writes in place so a
symlinked config stays a symlink.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument


class ConfigError(Exception):
    """The config file is invalid (CLI exit 2; the TUI shows it and doesn't start)."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class UiConfig:
    theme: str = "textual-dark"
    diff_context: int = 3
    narrow_breakpoint: int = 100


@dataclass(frozen=True)
class RefreshConfig:
    watch: bool = True
    debounce_ms: int = 300


@dataclass(frozen=True)
class SecretsConfig:
    # Globs and patterns stay raw strings: matching and `~` belong to masking.
    mask_patterns: tuple[str, ...] = ("token", "secret", "password", "passwd", "api[_-]?key")
    token_formats: bool = True
    never_mask: tuple[str, ...] = ("~/.config/karabiner/**", "~/.config/ghostty/config")
    always_mask: tuple[str, ...] = ("~/.netrc", "~/.ssh/id_*", "**/.env")


_TABLES = {"ui": UiConfig, "refresh": RefreshConfig, "secrets": SecretsConfig}

# The settings `Config.set` takes: the ones the settings screen edits in place.
SCALAR_KEYS = (
    "ui.theme",
    "ui.diff_context",
    "ui.narrow_breakpoint",
    "refresh.watch",
    "refresh.debounce_ms",
    "secrets.token_formats",
)


def config_path(environ: Mapping[str, str] = os.environ) -> Path:
    """`$XDG_CONFIG_HOME/cheztui/config.toml`, or `~/.config/…` when it's unset or empty."""
    xdg = environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path(environ.get("HOME") or Path.home()) / ".config"
    return base / "cheztui" / "config.toml"


def _is_int_at_least(value: object, minimum: int) -> bool:
    # A boolean is not an integer here (spec §9), though Python says it is.
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _check(key: str, value: object) -> Any:
    """Validate one known setting and return its typed value.

    A bad value raises `ValueError` saying what the setting must be.
    """
    match key:
        case "ui.theme":
            if not (isinstance(value, str) and value):
                raise ValueError(f"{key} must be a non-empty string")
        case "ui.diff_context" | "refresh.debounce_ms":
            if not _is_int_at_least(value, 0):
                raise ValueError(f"{key} must be an integer ≥ 0")
        case "ui.narrow_breakpoint":
            if not _is_int_at_least(value, 1):
                raise ValueError(f"{key} must be an integer ≥ 1")
        case "refresh.watch" | "secrets.token_formats":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        case "secrets.mask_patterns" | "secrets.never_mask" | "secrets.always_mask":
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise ValueError(f"{key} must be a list of strings")
            if key == "secrets.mask_patterns":
                for i, pattern in enumerate(value):
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        raise ValueError(
                            f"{key}[{i}] is not a valid regular expression: {e}"
                        ) from None
            return tuple(value)
        case _:
            raise KeyError(key)
    return value


def _section(path: Path, name: str, data: object) -> Any:
    cls = _TABLES[name]
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(path, f"{name} must be a table")
    values = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            try:
                values[f.name] = _check(f"{name}.{f.name}", data[f.name])
            except ValueError as e:
                raise ConfigError(path, str(e)) from None
    return cls(**values)


@dataclass
class Config:
    path: Path
    ui: UiConfig = dataclasses.field(default_factory=UiConfig)
    refresh: RefreshConfig = dataclasses.field(default_factory=RefreshConfig)
    secrets: SecretsConfig = dataclasses.field(default_factory=SecretsConfig)
    doc: TOMLDocument = dataclasses.field(default_factory=tomlkit.document, repr=False)

    def set(self, key: str, value: object) -> None:
        """Change one scalar setting, in the typed config and the document."""
        if key not in SCALAR_KEYS:
            raise ConfigError(self.path, f"{key} can't be set here: edit the file for it")
        try:
            value = _check(key, value)
        except ValueError as e:
            raise ConfigError(self.path, str(e)) from None
        table, name = key.split(".")
        setattr(self, table, dataclasses.replace(getattr(self, table), **{name: value}))
        if table not in self.doc:
            self.doc[table] = tomlkit.table()
        self.doc[table][name] = value

    def change(self, key: str, value: object) -> None:
        """Set one scalar setting and save it (spec §9).

        A save that fails puts the setting back, in the typed config and the
        document, before the `OSError` propagates: the screen, the file and
        what is in effect keep the previous value (#32).
        """
        tables = (self.ui, self.refresh, self.secrets)
        text = tomlkit.dumps(self.doc)
        self.set(key, value)
        try:
            self.save()
        except OSError:
            self.ui, self.refresh, self.secrets = tables
            self.doc = tomlkit.parse(text)
            raise

    def save(self) -> None:
        """Write the document in place (no temp-and-rename), so a symlink survives.

        Opening the file empties it, so a write that then fails (a full disk)
        puts the previous contents back as best it can.
        """
        text = tomlkit.dumps(self.doc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous: bytes | None = self.path.read_bytes()
        except OSError:
            previous = None
        try:
            with self.path.open("w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            if previous is not None:
                with contextlib.suppress(OSError):
                    self.path.write_bytes(previous)
            raise


def load(path: Path | None = None) -> Config:
    """Load the config file; a missing file gives the defaults and creates nothing."""
    path = path if path is not None else config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config(path)
    except OSError as e:
        raise ConfigError(path, f"can't read the file: {e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise ConfigError(path, f"not valid TOML: {e}") from None
    try:
        doc = tomlkit.parse(text)
    except TOMLKitError as e:
        raise ConfigError(path, f"not valid TOML: {e}") from None
    data = doc.unwrap()
    return Config(
        path,
        ui=_section(path, "ui", data.get("ui")),
        refresh=_section(path, "refresh", data.get("refresh")),
        secrets=_section(path, "secrets", data.get("secrets")),
        doc=doc,
    )
