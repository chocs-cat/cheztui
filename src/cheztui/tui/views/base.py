"""The list and detail panes every view fills (spec §4; DEC-2, DEC-33)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Generic, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from cheztui.tui.keys import Key

Row = TypeVar("Row")

SELECTED = "●"


class RowList(OptionList, inherit_bindings=False):
    """The list pane. Its keys are the app's (spec §5 Keys), so it has none of its own."""


class Detail(VerticalScroll, inherit_bindings=False):
    """The detail pane; it scrolls on the app's keys, and takes focus only when stacked."""

    can_focus = False


class FilterInput(Input):
    """The one-line filter under the list (spec §4 List)."""

    BINDINGS = [Binding("escape", "clear", show=False)]

    def action_clear(self) -> None:
        self.value = ""
        self.post_message(self.Submitted(self, self.value))


class ListDetail(Container, Generic[Row]):
    """A list of rows keyed by target, and the current row's detail.

    The rows are `None` before the view's read succeeds (blank), or a
    sequence, empty when there's nothing to list; `message` replaces them
    with a line such as a read's error. The filter and the selection
    belong to the list: the selection is a set of targets (spec §4 List).
    """

    NAME: ClassVar[str] = ""
    KEYS: ClassVar[tuple[Key, ...]] = ()
    EMPTY: ClassVar[str] = ""  # the one line shown with no rows

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: tuple[Row, ...] | None = None
        self.message: str | None = None
        self.filter = ""
        self.selected: set[Path] = set()
        self.shown: tuple[Row, ...] = ()  # the rows the filter shows, in list order

    # What a view says about its rows.

    def key(self, row: Row) -> Path:
        raise NotImplementedError

    def label(self, row: Row) -> str:
        raise NotImplementedError

    def detail(self, row: Row) -> str:
        raise NotImplementedError

    # The panes.

    def compose(self) -> ComposeResult:
        with Container(id="panes"):
            with Vertical(id="list-pane"):
                yield RowList(id="list")
                yield Static(id="list-message", markup=False)
                yield FilterInput(id="filter", placeholder="filter", compact=True)
            with Detail(id="detail"):
                yield Static(id="detail-body", markup=False)

    @property
    def count(self) -> int | None:
        """The rows to show on the view's tab, or None for no count (spec §4 Tab label)."""
        return len(self.rows) if self.rows else None

    @property
    def has_rows(self) -> bool:
        return bool(self.rows)

    def show_rows(self, rows: Sequence[Row]) -> None:
        """New rows from a refresh: the selection keeps the targets still listed (spec §4)."""
        self.rows, self.message = tuple(rows), None
        self.selected &= {self.key(r) for r in self.rows}
        self._render_rows()

    def show_message(self, message: str | None) -> None:
        """Replace the rows with `message`, or with nothing at all when it's None."""
        self.rows, self.message = None, message
        self.selected.clear()
        self._render_rows()

    def current(self) -> Row | None:
        index = self.query_one(RowList).highlighted
        return self.shown[index] if index is not None and index < len(self.shown) else None

    def targets(self) -> list[Row]:
        """What an action takes: the selected rows the filter shows, in list order, else
        the current row (spec §4 List, DEC-33)."""
        chosen = [r for r in self.shown if self.key(r) in self.selected]
        if chosen:
            return chosen
        row = self.current()
        return [row] if row is not None else []

    def _matches(self, row: Row) -> bool:
        return self.filter.casefold() in self.label(row).casefold()

    def _render_rows(self) -> None:
        """Rebuild the list; the current row stays on its target, else at its position."""
        listing = self.query_one(RowList)
        current = self.current()
        position = listing.highlighted or 0
        self.shown = tuple(r for r in self.rows or () if self._matches(r))
        listing.clear_options()
        listing.add_options(Option(self._prompt(r), id=str(self.key(r))) for r in self.shown)
        if self.shown:
            keys = [self.key(r) for r in self.shown]
            if current is not None and self.key(current) in keys:
                position = keys.index(self.key(current))
            listing.highlighted = min(position, len(self.shown) - 1)
        empty = self.EMPTY if self.rows == () else ""
        self.query_one("#list-message", Static).update(self.message or empty)
        self.set_class(not self.rows, "-no-rows")
        self._render_detail()

    def _prompt(self, row: Row) -> Content:
        gutter = SELECTED if self.key(row) in self.selected else " "
        return Content.assemble(gutter, " ", self.label(row))

    def _render_detail(self) -> None:
        row = self.current()
        self.query_one("#detail-body", Static).update("" if row is None else self.detail(row))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._render_detail()

    # The list's keys, run by the app (spec §5 Keys).

    def move(self, step: int) -> None:
        """Move the current row by `step`, stopping at either end."""
        listing = self.query_one(RowList)
        if listing.option_count and listing.highlighted is not None:
            target = listing.highlighted + step
            listing.highlighted = max(0, min(target, listing.option_count - 1))

    def toggle_selected(self) -> None:
        row = self.current()
        if row is None:
            return
        self.selected ^= {self.key(row)}
        index = self.query_one(RowList).highlighted
        assert index is not None
        self.query_one(RowList).replace_option_prompt_at_index(index, self._prompt(row))

    def open_filter(self) -> None:
        field = self.query_one(FilterInput)
        field.display = True
        field.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.filter = event.value
        self._render_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """`enter` keeps the filter, `esc` clears it; either returns to the list."""
        event.stop()
        self.filter = event.value
        event.input.display = bool(self.filter)
        self._render_rows()
        self.query_one(RowList).focus()
