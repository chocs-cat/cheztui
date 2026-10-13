"""The Drift view's rows and detail (spec §6 Drift, *Rows and detail in the TUI*; DEC-34).

Each row shows its direction, status columns, target and tags. The detail
is a head (the target and the direction in words, the source, the default
fix), a rule and a body: chezmoi's diff, or a template's rendered output
with `t`. Bodies are read in one exclusive worker and kept per row until
the next refresh (DEC-6), each for the row value it was read with: a
refresh's row whose tags or source changed is read again, so a body is
always masked for the row on screen (DEC-34); `R` re-reads the one on screen unmasked, and the
clear text lives only in the pane (DEC-17, DEC-30). Reading, masking and
cutting are `ops.drift_body`'s; colouring and layout are here.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.content import Content

from cheztui import ops
from cheztui.chezmoi import ChezmoiError, ChezmoiFailed
from cheztui.ops import DIRECTION_LABELS, Direction, DriftBody, DriftRow, display_target
from cheztui.secrets import Masker
from cheztui.tui import keys
from cheztui.tui.keys import Key
from cheztui.tui.views.base import ListDetail
from cheztui.tui.widgets.diff import diff_content

if TYPE_CHECKING:
    from cheztui.tui.app import CheztuiApp

DIRECTION_STYLES: dict[Direction, str] = {
    "both": "$error",
    "you": "$warning",
    "src": "$success",
    "run": "$accent",
}
DIRECTION_WORDS: dict[Direction, str] = {
    "you": "you changed the target",
    "src": "the source changed",
    "both": "both sides changed",
    "run": "the script will run",
}
READING = "reading…"
NO_DIFF = "(no diff)"

TOGGLE = Key(
    "t",
    "Toggle the diff and the rendered output",
    (("t", "toggle_rendered"),),
    label="Rendered",
    palette=True,
)

# The actions, in key-bar order; M, x and A are in help and the palette only
# (spec §6 Drift, Keys and *Actions in the TUI*).
FIX = Key("enter", "Default fix for the row", (("enter", "fix"),), label="Fix", palette=True)
APPLY = Key("a", "Apply", (("a", "apply"),), label="Apply", palette=True)
READD = Key(
    "r",
    "Re-add (not on templates, directories, symlinks or deleted targets)",
    (("r", "re_add"),),
    label="Re-add",
    palette=True,
)
MERGE = Key(
    "m",
    "Merge in chezmoi's merge tool (not on directories, symlinks or rows with no target state)",
    (("m", "merge"),),
    label="Merge",
    palette=True,
)
EDIT = Key("e", "Edit source", (("e", "edit"),), label="Edit", palette=True)
FULL_DIFF = Key("d", "Full diff", (("d", "full_diff"),), label="Full diff", palette=True)
MERGE_SELECTION = Key(
    "M",
    "Merge through the selection, one chezmoi merge call",
    (("M", "merge_selection"),),
    palette=True,
)
FORGET = Key("x", "Forget", (("x", "forget"),), palette=True)
APPLY_ALL = Key("A", "Apply all", (("A", "apply_all"),), palette=True)

# What each write key prepares, over the targets or the current row (spec §6 *Prepare*).
TARGETS: dict[str, ops.Action] = {
    "apply": "apply",
    "re_add": "re-add",
    "forget": "forget",
    "merge_selection": "merge",
}
CURRENT: dict[str, ops.Action] = {"edit": "edit", "merge": "merge"}
WRITES = frozenset({*TARGETS, *CURRENT, "fix", "apply_all"})

# The pane's body: a target, the rendered output (not the diff), revealed.
BodyKey = tuple[Path, bool, bool]


class DriftView(ListDetail[DriftRow]):
    NAME = "Drift"
    EMPTY = "✓ in sync"
    KEYS = (
        FIX,
        APPLY,
        READD,
        MERGE,
        EDIT,
        FULL_DIFF,
        TOGGLE,
        MERGE_SELECTION,
        FORGET,
        APPLY_ALL,
    )
    BINDINGS = keys.bindings(KEYS)

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Masked only, until a refresh; each with the row it was read for.
        self.bodies: dict[tuple[Path, bool], tuple[DriftRow, DriftBody]] = {}
        self.rendered = False  # `t`: the rendered output is on screen
        self.revealing = False  # `R`: the body on screen is read unmasked
        self._revealed: str | None = None  # that clear text, in the pane alone
        self._failed: tuple[BodyKey, str] | None = None  # a failed read's pane line
        self._reading: BodyKey | None = None  # the read in flight
        self._read_id = 0
        self._row: DriftRow | None = None  # the row the detail shows
        self._dest: asyncio.Future[Path] | None = None  # this refresh's destination

    @property
    def cheztui(self) -> CheztuiApp:
        return cast("CheztuiApp", self.app)

    # --- Rows (spec §6 Row) ---------------------------------------------------------

    def key(self, row: DriftRow) -> Path:
        return row.target

    def label(self, row: DriftRow) -> str:
        return display_target(row.target)

    def content(self, row: DriftRow) -> Content:
        """Direction, status columns, target, tags; the word carries the meaning (DEC-7)."""
        direction = DIRECTION_LABELS[row.direction].ljust(4)
        parts: list[Content | str] = [
            Content.styled(direction, DIRECTION_STYLES[row.direction]),
            f" {row.columns}  {self.label(row)}",
        ]
        if row.tags:
            parts += ["  ", Content.styled(" ".join(row.tags), "$text-muted")]
        return Content.assemble(*parts)

    # --- The head (spec §6 Detail head) ---------------------------------------------

    def head(self, row: DriftRow) -> Content:
        words = (
            "the target was deleted" if row.columns[0] == "D" else DIRECTION_WORDS[row.direction]
        )
        source = display_target(row.source) if row.source is not None else "(none)"
        fix = row.default_fix + (" (overwrites your change)" if row.fix_overwrites else "")
        parts: list[Content | str] = [
            f"{self.label(row)}  {words}\nsource  {source}\nfix     {fix}"
        ]
        if self._revealed is not None:
            parts += ["  ", Content.styled("revealed", "$warning")]
        return Content.assemble(*parts)

    # --- The body and its reads (spec §6 Detail reads) ------------------------------

    def _pane(self, row: DriftRow) -> BodyKey:
        return (row.target, self.rendered, self.revealing)

    def _render_detail(self) -> None:
        row = self.current()
        if row != self._row:
            # Another row, none, or a refresh's new value for this one: the diff again,
            # masked (spec §6 Body: rendered, Reveal), and nothing read for the old value.
            self._drop()
            self.rendered = False
        self._row = row
        if row is None:
            self.show_detail(None, "")
            return
        # Start the read before drawing: a worker can finish before `run_worker`
        # returns (eager tasks), and the pane must show its result, not `reading…`.
        self._ensure_read(row)
        self.show_detail(self.head(row), self._body(row))

    def _shown_text(self, row: DriftRow) -> str | None:
        return self._revealed if self.revealing else self._cached(row)

    def _ensure_read(self, row: DriftRow) -> None:
        pane = self._pane(row)
        failed = self._failed is not None and self._failed[0] == pane
        if not failed and self._shown_text(row) is None and self._reading != pane:
            self._start(row)

    def _body(self, row: DriftRow) -> Content | str:
        """What the body shows now."""
        pane = self._pane(row)
        if self._failed is not None and self._failed[0] == pane:
            return Content.styled(f"✗ {self._failed[1]}", "$error")
        text = self._shown_text(row)
        if text is None:
            return READING
        if not self.rendered:
            return diff_content(text) if text else NO_DIFF
        return Content(text.removesuffix("\n"))

    def _cached_body(self, row: DriftRow) -> DriftBody | None:
        """The kept body, if it was read for this row value (DEC-34)."""
        kept = self.bodies.get((row.target, self.rendered))
        return kept[1] if kept is not None and kept[0] == row else None

    def _cached(self, row: DriftRow) -> str | None:
        body = self._cached_body(row)
        return body.text if body is not None else None

    def _start(self, row: DriftRow) -> None:
        self._read_id += 1
        self._reading = self._pane(row)
        self.run_worker(
            self._read(row, self.rendered, self.revealing, self._read_id),
            group="body",
            exclusive=True,
        )

    def _drop(self) -> None:
        """Cancel the body read, silently, and drop what's revealed or failed."""
        self.workers.cancel_group(self, "body")
        self._read_id += 1
        self._reading = None
        self.revealing = False
        self._revealed = None
        self._failed = None

    def reset_detail(self) -> None:
        """A refresh: the bodies and the destination are read again (spec §6, DEC-6)."""
        self.bodies.clear()
        if self._dest is not None:
            self._dest.cancel()
            self._dest = None
        self._drop()
        self.rendered = False
        self._render_detail()

    def _destination(self) -> asyncio.Future[Path]:
        """The effective destination, read once per refresh (DEC-26); a moved-away
        read leaves it running for the next one."""
        if self._dest is None:

            async def read() -> Path:
                return (await self.cheztui.chezmoi.dump_config()).dest_dir

            self._dest = asyncio.ensure_future(read())
            # Retrieved here too, so a failure no read is waiting for isn't reported.
            self._dest.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return self._dest

    async def _read(self, row: DriftRow, rendered: bool, reveal: bool, read_id: int) -> None:
        app = self.cheztui
        # Read diagnostics: line level, no never_mask exemption (spec §5, DEC-33).
        masker = Masker(app.settings.secrets, Path("/"))
        try:
            dest = await ops.sanitized(asyncio.shield(self._destination()), masker, whole=False)
            body = await ops.sanitized(
                ops.drift_body(
                    app.chezmoi,
                    app.settings.secrets,
                    row,
                    dest,
                    context=app.settings.ui.diff_context,
                    rendered=rendered,
                    reveal=reveal,
                ),
                masker,
                whole=False,
            )
        except ChezmoiError as e:
            if read_id != self._read_id:
                return
            message = ops.mask_diagnostic(masker, ops.failure_message(e), whole=False)
            stderr = e.stderr if isinstance(e, ChezmoiFailed) else ""
            self._reading = None
            self._failed = ((row.target, rendered, reveal), message)
            # Logged as it's shown; the log line belongs to refreshes and writes (DEC-34).
            app.record(f"✗ {message}", stderr.rstrip("\n") or message)
            self._render_detail()
            return
        if read_id != self._read_id:
            return
        self._reading = None
        if reveal:
            self._revealed = body.text
        else:
            self.bodies[(row.target, rendered)] = (row, body)
        self._render_detail()

    def on_unmount(self) -> None:
        if self._dest is not None:
            with contextlib.suppress(Exception):
                self._dest.cancel()

    # --- `t` and `R` (spec §6 Body: rendered, Reveal) --------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_rendered":
            row = self.current()
            return row is not None and "tmpl" in row.tags
        if action in WRITES or action == "full_diff":
            return self.can_write(action)
        return True

    # --- The actions (spec §6 Drift, *Actions in the TUI*; DEC-35) ----------------------

    def can_write(self, action: str) -> bool:
        """A key works when `ops.eligibility` admits a row it would take, and no
        write is being prepared or run (spec §6 Keys)."""
        if self.cheztui.writing is not None:
            return False
        if action in TARGETS:
            return any(ops.eligibility(TARGETS[action], r) is None for r in self.targets())
        row = self.current()
        if action in CURRENT:
            return row is not None and ops.eligibility(CURRENT[action], row) is None
        if action == "apply_all":
            return self.has_rows
        return row is not None  # enter, d

    def _prepare(self, action: str) -> None:
        if not self.can_write(action):
            return
        writes = self.cheztui.writes
        # The rows are taken now: a refresh landing while the plan is prepared
        # doesn't change what it names (spec §6 *Prepare*).
        if action in TARGETS:
            rows = self.targets()
            self.cheztui.start_write(lambda: writes.prepare(TARGETS[action], rows))
        elif action in CURRENT:
            row = self.current()
            assert row is not None
            self.cheztui.start_write(lambda: writes.prepare(CURRENT[action], [row]))
        elif action == "fix":
            rows = self.targets()
            self.cheztui.start_write(lambda: writes.prepare_defaults(rows))
        else:
            every = list(self.rows or ())  # whatever the filter and selection show
            self.cheztui.start_write(lambda: writes.prepare_apply_all(every))

    def action_fix(self) -> None:
        self._prepare("fix")

    def action_apply(self) -> None:
        self._prepare("apply")

    def action_re_add(self) -> None:
        self._prepare("re_add")

    def action_merge(self) -> None:
        self._prepare("merge")

    def action_edit(self) -> None:
        self._prepare("edit")

    def action_merge_selection(self) -> None:
        self._prepare("merge_selection")

    def action_forget(self) -> None:
        self._prepare("forget")

    def action_apply_all(self) -> None:
        self._prepare("apply_all")

    def action_full_diff(self) -> None:
        """`d`: chezmoi's own diff in the terminal (spec §6 `d`)."""

    def key_label(self, key: Key) -> str:
        if key is TOGGLE:
            return "Diff" if self.rendered else "Rendered"
        return key.label

    def action_toggle_rendered(self) -> None:
        row = self.current()
        if row is None or "tmpl" not in row.tags:
            return
        self._drop()
        self.rendered = not self.rendered
        self._render_detail()

    @property
    def can_reveal(self) -> bool:
        """The body on screen had something masked, or is revealed (spec §6 Reveal)."""
        row = self.current()
        if row is None:
            return False
        if self.revealing:
            return True
        body = self._cached_body(row)
        return body is not None and body.masked and self._failed is None

    def toggle_reveal(self) -> None:
        if not self.can_reveal:
            return
        reveal = not self.revealing
        self._drop()
        self.revealing = reveal
        self._render_detail()
