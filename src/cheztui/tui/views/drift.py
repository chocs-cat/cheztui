"""The Drift view (spec §6 Drift; DEC-1).

A skeleton for now: each row is its target, and the detail its target and
source. Glyphs, tags, the diff and the actions come with the rest of v0.1.
"""

from __future__ import annotations

from pathlib import Path

from cheztui.ops import DriftRow, display_target
from cheztui.tui.views.base import ListDetail


class DriftView(ListDetail[DriftRow]):
    NAME = "Drift"
    EMPTY = "✓ in sync"

    def key(self, row: DriftRow) -> Path:
        return row.target

    def label(self, row: DriftRow) -> str:
        return display_target(row.target)

    def detail(self, row: DriftRow) -> str:
        source = display_target(row.source) if row.source is not None else "(no source)"
        return f"{display_target(row.target)}\n{source}"
