"""The settings screen (spec §9; DEC-33).

One row per scalar setting. A change is validated as the file would be,
saved at once and applied at once; an invalid value shows its error on its
row and isn't saved. `e` opens the file in the user's editor and reloads it.
`-n` doesn't apply: it covers chezmoi's calls, not cheztui's own config.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, OptionList, Static
from textual.widgets.option_list import Option

from cheztui.config import SCALAR_KEYS, ConfigError

if TYPE_CHECKING:
    from cheztui.tui.app import CheztuiApp


def launch_editor(path: Path) -> None:
    """Run `$VISUAL`, else `$EDITOR`, else `vi`, split as shell words, on `path`."""
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(command), str(path)], check=False)


def shown(value: object) -> str:
    """A value as the TOML file spells it."""
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


class ValueInput(Input):
    """The input `enter` opens on an integer's row; `esc` leaves the value as it was."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def action_cancel(self) -> None:
        row = next(a for a in self.ancestors if isinstance(a, SettingRow))
        list_view = self.screen.query_one(ListView)
        self.remove()
        row.editing = False
        row.refresh_value()
        list_view.focus()


class SettingRow(ListItem):
    def __init__(self, key: str) -> None:
        super().__init__(id=f"setting-{key.replace('.', '-')}")
        self.key = key
        self.error = ""
        self.editing = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(self.key, classes="setting-key", markup=False)
            yield Static(classes="setting-value", markup=False)
            yield Static(classes="setting-error", markup=False)

    def on_mount(self) -> None:
        self.refresh_value()

    @property
    def value(self) -> object:
        table, name = self.key.split(".")
        settings = cast("CheztuiApp", self.app).settings
        return getattr(getattr(settings, table), name)

    def refresh_value(self) -> None:
        self.query_one(".setting-value", Static).update(shown(self.value))
        self.query_one(".setting-error", Static).update(f"✗ {self.error}" if self.error else "")


class ThemePicker(ModalScreen[str | None]):
    """The themes Textual knows, to choose `ui.theme` from (spec §9)."""

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = """
    ThemePicker { align: center middle; }
    ThemePicker OptionList { width: 40; height: auto; max-height: 80%; }
    """

    def __init__(self, themes: list[str], current: str) -> None:
        super().__init__()
        self.themes = themes
        self.current = current

    def compose(self) -> ComposeResult:
        picker = OptionList(*(Option(t, id=t) for t in self.themes))
        picker.border_title = "ui.theme"
        yield picker

    def on_mount(self) -> None:
        picker = self.query_one(OptionList)
        if self.current in self.themes:
            picker.highlighted = self.themes.index(self.current)
        picker.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("e", "edit_file", show=False),
    ]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical {
        width: 90%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $background;
        padding: 0 1;
    }
    SettingsScreen ListView { height: auto; background: $background; }
    SettingsScreen SettingRow Horizontal { height: 1; }
    SettingsScreen .setting-key { width: 25; }
    SettingsScreen .setting-value { width: auto; min-width: 16; }
    SettingsScreen .setting-error { width: 1fr; color: $error; padding-left: 2; }
    SettingsScreen ValueInput { width: 16; height: 1; border: none; padding: 0; }
    SettingsScreen #settings-error { color: $error; height: auto; }
    SettingsScreen #settings-hint { color: $text-muted; }
    """

    @property
    def cheztui(self) -> CheztuiApp:
        return cast("CheztuiApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = "Settings"
            yield Static(str(self.cheztui.settings.path), markup=False, id="settings-path")
            yield ListView(*(SettingRow(key) for key in SCALAR_KEYS))
            yield Static(id="settings-error", markup=False)
            yield Static("enter change  e edit the file  esc close", id="settings-hint")

    def row(self, key: str) -> SettingRow:
        return self.query_one(f"#setting-{key.replace('.', '-')}", SettingRow)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        row = event.item
        assert isinstance(row, SettingRow)
        value = row.value
        if isinstance(value, bool):
            self.change(row.key, not value)
        elif row.key == "ui.theme":
            from cheztui.tui.app import THEMES

            def chosen(theme: str | None) -> None:
                if theme is not None:
                    self.change(row.key, theme)

            self.app.push_screen(ThemePicker(sorted(THEMES), str(value)), chosen)
        elif not row.editing:
            row.editing = True
            row.query_one(".setting-value", Static).update("")
            field = ValueInput(shown(value), select_on_focus=True)
            row.query_one(Horizontal).mount(field, after=row.query_one(".setting-value"))
            field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        row = next(a for a in event.input.ancestors if isinstance(a, SettingRow))
        text = event.value.strip()
        try:
            value: object = int(text)
        except ValueError:
            value = text  # not an integer: Config.set says what it must be
        event.input.remove()
        row.editing = False
        self.change(row.key, value)
        self.query_one(ListView).focus()

    def change(self, key: str, value: object) -> None:
        """Validate, save and apply one setting; an error stays on its row, unsaved.

        A save that fails leaves the previous value in the config and the file,
        and nothing is applied (spec §9, #32).
        """
        row = self.row(key)
        settings = self.cheztui.settings
        try:
            settings.change(key, value)
        except ConfigError as e:
            row.error = e.message
        except OSError as e:
            row.error = f"can't save: {e.strerror or e}"
        else:
            row.error = ""
            self.cheztui.apply_settings()
        row.refresh_value()

    def action_edit_file(self) -> None:
        """Edit the file in `$VISUAL`/`$EDITOR`/`vi`, then reload it (spec §9)."""
        error = self.cheztui.edit_config()
        self.query_one("#settings-error", Static).update(f"✗ {error}" if error else "")
        for row in self.query(SettingRow):
            row.error = ""
            row.refresh_value()

    def action_close(self) -> None:
        self.dismiss()
