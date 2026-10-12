"""The TUI's key declarations (spec §5 Keys; DEC-3).

One list per scope: the global keys here, in the order of the spec's table,
and each view's own in its `KEYS`. The Textual bindings, the key bar, help
and the palette are all generated from them, so they can't disagree.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from textual.binding import Binding


@dataclass(frozen=True)
class Key:
    keys: str  # as help, the palette and the key bar show it: "j/k ↑/↓"
    description: str  # help's text for it
    bindings: tuple[tuple[str, str], ...]  # (Textual key list, action) pairs
    label: str = ""  # the key bar's word; empty: never on the key bar
    bar: int = 0  # the global keys' place on the key bar, after the view's (spec §4)
    palette: bool = False  # offered by the palette, which runs the first binding's action

    @property
    def action(self) -> str:
        return self.bindings[0][1]


GLOBAL: tuple[Key, ...] = (
    Key("1", "Drift view", (("1", "view('drift')"),)),
    Key("j/k ↑/↓", "Move", (("j,down", "move(1)"), ("k,up", "move(-1)"))),
    Key(
        "J/K PgUp/PgDn",
        "Scroll detail",
        (
            ("J", "scroll_detail(1)"),
            ("K", "scroll_detail(-1)"),
            ("pagedown", "page_detail(1)"),
            ("pageup", "page_detail(-1)"),
        ),
    ),
    Key(
        "/",
        "Filter list (substring of the target path; esc clears)",
        (("slash", "filter"),),
        label="Filter",
        bar=2,
        palette=True,
    ),
    Key(
        "space",
        "Toggle selection (actions apply to selection, else to the current row)",
        (("space", "select"),),
        label="Select",
        bar=1,
        palette=True,
    ),
    Key("g", "Refresh", (("g", "refresh"),), palette=True),
    Key(":", "Command palette", (("colon", "command_palette"),)),
    Key("l", "Session log", (("l", "session_log"),), palette=True),
    Key(",", "Settings", (("comma", "settings"),), palette=True),
    Key("?", "Help", (("question_mark", "help"),), label="Help", bar=3, palette=True),
    Key("q", "Quit", (("q", "quit"),), palette=True),
    Key("esc", "Cancel the read in progress", (("escape", "cancel_read"),)),
    Key("tab", "Switch focus between list and detail (narrow layout)", (("tab", "toggle_focus"),)),
)


def bindings(keys: Iterable[Key]) -> list[Binding]:
    """Textual bindings for `keys`, hidden from Textual's own footer: the key bar is ours."""
    return [Binding(key, action, show=False) for k in keys for key, action in k.bindings]
