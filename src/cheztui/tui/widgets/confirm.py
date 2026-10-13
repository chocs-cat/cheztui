"""The preview modal over a `WritePlan` (spec §6 Drift, *The preview modal*; DEC-4, DEC-35).

Top to bottom: each command's `display` after `$ `; the entries grouped by
action under its name; the stray-file warning; the current entry's masked
diff, coloured and cut as in the detail. A blocked plan shows its reason
instead and only closes. It runs nothing: `enter` dismisses with the plan
(re-ticked for apply-all, `WritePlan.with_selection`), `esc` with None.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

from cheztui import ops
from cheztui.ops import DIRECTION_LABELS, WriteEntry, WritePlan, display_target
from cheztui.tui.widgets.diff import diff_content

NO_PREVIEW = "(no preview)"
NO_DIFF = "(no diff)"
STRAY_WARNING = "warning: uncommitted files in the source tree:"
BLOCKED = "✗ the source tree has uncommitted changes; commit or remove them first:"
NOTHING_TICKED = "nothing ticked"


def entry_line(entry: WriteEntry, *, apply_all: bool) -> Content:
    """Two spaces, the tick for apply-all, the direction as the Drift row shows it,
    the target, then what applies: overwrites, left out, or the directory (spec §6)."""
    tick = ("[x] " if entry.selected else "[ ] ") if apply_all else ""
    direction = DIRECTION_LABELS[entry.direction] if entry.direction is not None else ""
    parts: list[Content | str] = [f"  {tick}{direction.ljust(4)} {display_target(entry.target)}"]
    if entry.overwrites:
        parts += [" ", Content.styled("overwrites your change", "$warning")]
    if entry.excluded is not None:
        parts += [" ", Content.styled(f"left out: {entry.excluded}", "$text-muted")]
    elif entry.implied_by is not None:
        parts.append(f" with {display_target(entry.implied_by)}")
    return Content.assemble(*parts)


def headings(plan: WritePlan) -> list[tuple[str, list[int]]]:
    """Each action's heading and its entries' indexes, in execution order (spec §6).

    With non-interactive entries before them, the merges run suspended after
    the modal's commands: `then merge, suspended:`.
    """
    groups: list[tuple[str, list[int]]] = []
    for i, entry in enumerate(plan.entries):
        if not groups or groups[-1][0] != entry.action:
            groups.append((entry.action, []))
        groups[-1][1].append(i)
    mixed = any(e.action not in ops.INTERACTIVE for e in plan.entries)
    return [
        ("then merge, suspended:" if action == "merge" and mixed else action, indexes)
        for action, indexes in groups
    ]


class PreviewModal(ModalScreen[WritePlan | None]):
    """A plan's commands, entries and the current entry's diff, then `enter` or `esc`."""

    DEFAULT_CSS = """
    PreviewModal { align: center middle; }
    PreviewModal > Vertical {
        width: 90%;
        height: 90%;
        border: round $primary;
        background: $background;
        padding: 0 1;
    }
    #preview-body { height: 1fr; }
    #preview-body > Static { width: 1fr; }
    #preview-entries { height: auto; }
    .entry.-current {
        color: $block-cursor-foreground;
        background: $block-cursor-background;
        text-style: $block-cursor-text-style;
    }
    #preview-commands, #preview-entries, #preview-stray { margin-bottom: 1; }
    #preview-footer { height: 1; dock: bottom; }
    """

    BINDINGS = [
        Binding("enter", "run", show=False),
        Binding("escape", "cancel", show=False),
        Binding("space", "tick", show=False),
        Binding("j,down", "move(1)", show=False),
        Binding("k,up", "move(-1)", show=False),
        Binding("J", "scroll(1)", show=False),
        Binding("K", "scroll(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
        Binding("pageup", "page(-1)", show=False),
    ]

    def __init__(self, plan: WritePlan, *, context: int) -> None:
        super().__init__()
        self.plan = plan
        self.context = context  # `ui.diff_context` (spec §6)
        self.current = 0

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = "Preview"
            with VerticalScroll(id="preview-body"):
                yield Static(id="preview-commands", markup=False)
                with Vertical(id="preview-entries"):
                    for title, indexes in headings(self.plan):
                        yield Static(title, markup=False, classes="heading")
                        for i in indexes:
                            yield Static(id=f"entry-{i}", markup=False, classes="entry")
                yield Static(id="preview-stray", markup=False)
                yield Static(id="preview-diff", markup=False)
            yield Static(id="preview-footer", markup=False)

    def on_mount(self) -> None:
        self._render_plan()

    # --- Rendering ------------------------------------------------------------------

    def _render_plan(self) -> None:
        plan = self.plan
        commands = "\n".join(f"$ {c.display}" for c in plan.commands)
        self.query_one("#preview-commands", Static).update(commands)
        self.query_one("#preview-commands").display = bool(plan.commands)
        for i, entry in enumerate(plan.entries):
            line = self.query_one(f"#entry-{i}", Static)
            line.update(entry_line(entry, apply_all=plan.apply_all))
            line.set_class(i == self.current, "-current")
        stray = self.query_one("#preview-stray", Static)
        if plan.preflight.blocked:
            paths = "".join(f"\n  {p}" for p in plan.preflight.stray)
            stray.update(Content.assemble(Content.styled(BLOCKED, "$error"), paths))
        elif plan.error is not None:
            stray.update(Content.styled(f"✗ preview failed: {plan.error}", "$error"))
        elif plan.preflight.warns:
            stray.update(STRAY_WARNING + "".join(f"\n  {p}" for p in plan.preflight.stray))
        stray.display = plan.blocked or plan.preflight.warns
        self._render_diff()
        self.query_one("#preview-footer", Static).update(self._footer())

    def _render_diff(self) -> None:
        diff = self.query_one("#preview-diff", Static)
        diff.display = not self.plan.blocked and bool(self.plan.entries)
        if not diff.display:
            return
        text = self.plan.entries[self.current].diff
        if text is None:
            diff.update(NO_PREVIEW)
        elif not text:
            diff.update(NO_DIFF)
        else:
            diff.update(diff_content(ops.cut_context(text, self.context)))

    def _footer(self) -> Content:
        if self.plan.blocked:
            keys = [("esc", "Close")]
        else:
            run = ("enter", "Dry run" if self.plan.dry_run else "Run")
            keys = [
                *([run] if self.plan.commands else []),
                *([("space", "Tick")] if self.plan.apply_all else []),
                ("j/k", "Move"),
                ("esc", "Cancel"),
            ]
        parts: list[Content | str] = (
            [] if self.plan.commands or self.plan.blocked else [NOTHING_TICKED]
        )
        for key, label in keys:
            if parts:
                parts.append("  ")
            parts += [Content.styled(key, "bold $accent"), f" {label}"]
        return Content.assemble(*parts)

    # --- Keys -----------------------------------------------------------------------

    def action_move(self, step: int) -> None:
        """Move the current entry, stopping at either end."""
        if not self.plan.entries:
            return
        self.current = max(0, min(self.current + step, len(self.plan.entries) - 1))
        for i in range(len(self.plan.entries)):
            self.query_one(f"#entry-{i}").set_class(i == self.current, "-current")
        self.query_one(f"#entry-{self.current}").scroll_visible(animate=False)
        self._render_diff()

    def action_scroll(self, step: int) -> None:
        self.query_one("#preview-body").scroll_relative(y=step, animate=False)

    def action_page(self, step: int) -> None:
        body = self.query_one("#preview-body", VerticalScroll)
        if step > 0:
            body.scroll_page_down(animate=False)
        else:
            body.scroll_page_up(animate=False)

    def action_tick(self) -> None:
        """Apply-all's `space`: tick or untick the current entry (spec §6; DEC-16)."""
        plan = self.plan
        if not plan.apply_all or plan.blocked or not plan.entries:
            return
        entry = plan.entries[self.current]
        if entry.excluded is not None or entry.implied_by is not None:
            return
        ticked = {e.target for e in plan.entries if e.selected and e.implied_by is None}
        self.plan = plan.with_selection(ticked ^ {entry.target})
        self._render_plan()

    def action_run(self) -> None:
        if self.plan.blocked or not self.plan.commands:
            return  # nothing ticked: `enter` does nothing
        self.dismiss(self.plan)

    def action_cancel(self) -> None:
        self.dismiss(None)
