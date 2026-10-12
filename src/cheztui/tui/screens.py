"""Help, the command palette and the session log (spec §5 Help, palette, session log; DEC-3).

Help and the palette are generated from the key declarations in `keys.py`,
as the key bar is, so the three can't disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits, Provider, SearchIcon
from textual.containers import VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

from cheztui.tui.keys import Key

if TYPE_CHECKING:
    from cheztui.tui.app import CheztuiApp, LogEntry


def help_text(view: str, view_keys: Sequence[Key], global_keys: Sequence[Key]) -> str:
    """The view's keys under its name, then the global ones under `Global` (spec §5).

    A view with no keys of its own has no section. Each line is the key and its
    description, the keys padded to the widest in the section.
    """
    sections = []
    for title, section in ((view, view_keys), ("Global", global_keys)):
        if not section:
            continue
        width = max(len(k.keys) for k in section)
        lines = [f"{k.keys.ljust(width)}  {k.description}" for k in section]
        sections.append("\n".join([title, *lines]))
    return "\n\n".join(sections)


class TextScreen(ModalScreen[None]):
    """A modal page of text that closes on its keys."""

    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > VerticalScroll {
        width: 90%;
        height: 80%;
        border: round $primary;
        background: $background;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll() as scroll:
            scroll.border_title = self.title_text
            yield Static(self.text, markup=False, id="text")

    def action_close(self) -> None:
        self.dismiss()


class HelpScreen(TextScreen):
    BINDINGS = [Binding("escape,question_mark,q", "close", show=False)]

    def __init__(self, text: str) -> None:
        super().__init__("Help", text)


def session_log_text(entries: Sequence[LogEntry]) -> str:
    """Each entry `HH:MM:SS  <log-line text>`, an error's diagnostic indented two spaces."""
    lines = []
    for entry in entries:
        lines.append(f"{entry.time:%H:%M:%S}  {entry.text}")
        if entry.diagnostic is not None:
            lines += [f"  {line}" for line in entry.diagnostic.splitlines()]
    return "\n".join(lines)


class SessionLogScreen(TextScreen):
    """The session log, oldest first and scrolled to the newest (spec §5)."""

    BINDINGS = [Binding("escape,l,q", "close", show=False)]

    def __init__(self, entries: Sequence[LogEntry]) -> None:
        super().__init__("Session log", session_log_text(entries))

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).scroll_end(animate=False)


class Commands(Provider):
    """cheztui's palette entries: the keys that work now, each with its key (spec §5)."""

    @property
    def cheztui(self) -> CheztuiApp:
        return cast("CheztuiApp", self.app)

    def _entries(self) -> list[tuple[str, Key]]:
        return [(k.label or k.description, k) for k in self.cheztui.palette_keys]

    def _display(self, name: Content, key: Key) -> Content:
        return Content.assemble(name, "  ", (key.keys, "bold $accent"))

    async def discover(self) -> Hits:
        for name, key in self._entries():
            yield DiscoveryHit(
                self._display(Content(name), key),
                partial(self.cheztui.run_action, key.action),
                text=name,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, key in self._entries():
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    self._display(matcher.highlight(name), key),
                    partial(self.cheztui.run_action, key.action),
                    text=name,
                )


class Palette(CommandPalette):
    """Textual's palette with cheztui's entries alone, and no emoji (DEC-7)."""

    def __init__(self) -> None:
        super().__init__(providers=[Commands], placeholder="Run a command…")

    def on_mount(self) -> None:
        self.query_one(SearchIcon).icon = ":"
