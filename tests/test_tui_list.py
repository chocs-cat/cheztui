"""The list: movement, detail scrolling, filter and selection (spec §4 List, §5 Keys; DEC-33)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheztui.ops import DriftRow
from cheztui.tui.views.base import Detail, FilterInput, RowList
from cheztui.tui.views.drift import DriftView
from tests.fake_chezmoi import FakeChezmoi
from tests.test_chezmoi_reads import ROOT, fixture
from tests.test_tui import make_app, settled, text

HOME = ROOT / "home"


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(HOME))


def view(app) -> DriftView:
    return app.query_one(DriftView)


def current(app) -> str | None:
    row = view(app).current()
    return None if row is None else row.target.name


def shown(app) -> list[str]:
    """The list as displayed, gutter and target; tests/test_tui_drift.py has the rest of a row."""
    listing = app.query_one(RowList)
    rows = view(app).shown
    assert listing.option_count == len(rows)
    return [
        str(listing.get_option_at_index(i).prompt)[:2] + view(app).label(row)
        for i, row in enumerate(rows)
    ]


def names(rows: list[DriftRow]) -> list[str]:
    return [r.target.name for r in rows]


def status_without(*names: str) -> str:
    lines = fixture("status.txt").splitlines(keepends=True)
    return "".join(line for line in lines if Path(line[3:].rstrip("\n")).name not in names)


# --- Movement ------------------------------------------------------------------------


async def test_j_k_and_arrows_move_the_current_row(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert current(app) == ".both file"
        await pilot.press("j")
        assert current(app) == ".gone"
        await pilot.press("down", "down")
        assert current(app) == ".tmplfile"
        await pilot.press("k")
        assert current(app) == ".mine"
        assert text(app, "#detail-head").startswith("~/.mine  ")
        await pilot.press("up", "up", "up")
        assert current(app) == ".both file"  # it stops at the ends
        await pilot.press(*"jjjjjjj")
        assert current(app) == "hello.sh"


def long_detail() -> FakeChezmoi:
    """The first row's diff runs to a hundred lines."""
    fake = FakeChezmoi.drifted()
    lines = "".join(f"+line {i}\n" for i in range(100))
    fake.diff[HOME / ".both file"] = f"@@ -0,0 +1,100 @@\n{lines}"
    return fake


async def test_J_K_and_page_keys_scroll_the_detail(tmp_path):
    app = make_app(long_detail(), tmp_path)
    async with app.run_test(size=(120, 20)) as pilot:
        await settled(pilot)
        detail = app.query_one(Detail)
        await pilot.press("J", "J", "J")
        assert detail.scroll_y == 3
        await pilot.press("K")
        assert detail.scroll_y == 2
        await pilot.press("pagedown")
        page = detail.scrollable_content_region.height
        assert detail.scroll_y == 2 + page
        await pilot.press("pageup")
        assert detail.scroll_y == 2
        assert current(app) == ".both file"  # the list didn't move


async def test_tab_toggles_focus_when_narrow(tmp_path):
    app = make_app(long_detail(), tmp_path)
    async with app.run_test(size=(80, 32)) as pilot:
        await settled(pilot)
        detail = app.query_one(Detail)
        assert app.query_one(RowList).has_focus
        await pilot.press("tab")
        assert detail.has_focus
        await pilot.press("j", "j")
        assert (detail.scroll_y, current(app)) == (2, ".both file")
        await pilot.press("k")
        assert detail.scroll_y == 1
        await pilot.press("tab")
        assert app.query_one(RowList).has_focus
        await pilot.press("j")
        assert (detail.scroll_y, current(app)) == (1, ".gone")


async def test_tab_does_nothing_side_by_side(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test(size=(120, 32)) as pilot:
        await settled(pilot)
        await pilot.press("tab")
        assert app.query_one(RowList).has_focus
        await pilot.press("tab", "j")
        assert current(app) == ".gone"


async def test_widening_moves_focus_off_the_detail(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test(size=(80, 32)) as pilot:
        await settled(pilot)
        await pilot.press("tab")
        assert app.query_one(Detail).has_focus
        await pilot.resize_terminal(120, 32)
        await pilot.pause()
        assert app.query_one(RowList).has_focus


# --- Filter -----------------------------------------------------------------------------


async def test_filter_is_a_live_case_insensitive_substring(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("slash")
        assert app.query_one(FilterInput).has_focus
        await pilot.press(*"~/.MI")
        assert shown(app) == ["  ~/.mine"]
        assert current(app) == ".mine"
        await pilot.press("backspace", "backspace")
        assert shown(app) == [
            "  ~/.both file",
            "  ~/.gone",
            "  ~/.mine",
            "  ~/.tmplfile",
            "  ~/.tool",
        ]
        await pilot.press("enter")
        assert app.query_one(RowList).has_focus
        assert app.query_one(FilterInput).display
        assert len(shown(app)) == 5  # kept
        assert text(app, "#tabs").strip() == "1 Drift 6"  # the tab counts every row
        await pilot.press("slash", "escape")
        assert len(shown(app)) == 6
        assert not app.query_one(FilterInput).display
        assert app.query_one(RowList).has_focus


async def test_filter_keys_are_text_while_typing(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        calls = len(fake.log)
        await pilot.press("slash", "j", "g", "q", "space", "2")
        assert app.query_one(FilterInput).value == "jgq 2"
        assert len(fake.log) == calls  # `g` didn't refresh
        assert app.is_running


async def test_a_target_outside_home_filters_on_its_absolute_form(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/elsewhere")
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("slash", *"/SCRATCH/home/.to", "enter")
        assert shown(app) == ["  /scratch/home/.tool"]
        await pilot.press("slash", "ctrl+u", "~", "enter")
        assert shown(app) == []


async def test_esc_in_the_filter_clears_it_and_keeps_the_read(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("status")
        await pilot.press("slash", "m", "g")  # `g` is text here
        await pilot.press("escape")  # clears the filter; no read to cancel
        assert app.query_one(FilterInput).value == ""
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("slash", "x", "escape")
        assert app.reading
        assert text(app, "#log-line") == "reading chezmoi…"
        await pilot.press("escape")  # back on the list, esc cancels
        assert text(app, "#log-line") == "refresh cancelled"
        release.set()


# --- Selection ------------------------------------------------------------------------


async def test_space_toggles_the_gutter(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("space", "j", "j", "space")
        assert shown(app) == [
            "● ~/.both file",
            "  ~/.gone",
            "● ~/.mine",
            "  ~/.tmplfile",
            "  ~/.tool",
            "  ~/hello.sh",
        ]
        assert current(app) == ".mine"  # space doesn't move
        assert view(app).selected == {HOME / ".both file", HOME / ".mine"}
        await pilot.press("space")
        assert view(app).selected == {HOME / ".both file"}
        assert shown(app)[2] == "  ~/.mine"


async def test_targets_are_the_shown_selection_else_the_current_row(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("j", "j")
        assert names(view(app).targets()) == [".mine"]
        await pilot.press("j", "j", "j", "space", "k", "k", "k", "k", "space")
        assert names(view(app).targets()) == [".gone", "hello.sh"]  # list order
        # A selected row the filter hides isn't acted on, and comes back selected.
        await pilot.press("slash", *"hello", "enter")
        assert names(view(app).targets()) == ["hello.sh"]
        await pilot.press("slash", "ctrl+u", *"mine", "enter")
        assert names(view(app).targets()) == [".mine"]  # nothing selected shows: the current
        await pilot.press("slash", "escape")
        assert names(view(app).targets()) == [".gone", "hello.sh"]
        assert shown(app)[1] == "● ~/.gone"


async def test_a_refresh_keeps_the_selected_targets_still_listed(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("space", "j", "j", "space")
        fake.status = status_without(".mine")
        await pilot.press("g")
        await settled(pilot)
        assert view(app).selected == {HOME / ".both file"}
        fake.status = fixture("status.txt")
        await pilot.press("g")
        await settled(pilot)
        assert view(app).selected == {HOME / ".both file"}  # gone means deselected


async def test_a_failed_read_drops_the_selection(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("space")
        fake.fail("status", b"chezmoi: broke\n", binary="chezmoi")
        await pilot.press("g")
        await settled(pilot)
        assert view(app).selected == set()
        assert view(app).targets() == []


# --- The current row after a refresh -----------------------------------------------------


async def test_the_current_row_stays_on_its_target(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("j", "j")
        assert current(app) == ".mine"
        fake.status = status_without(".both file", ".gone")  # .mine moves up to the top
        await pilot.press("g")
        await settled(pilot)
        assert current(app) == ".mine"
        assert app.query_one(RowList).highlighted == 0


async def test_the_current_row_keeps_its_position_when_its_target_goes(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("j", "j")
        fake.status = status_without(".mine")
        await pilot.press("g")
        await settled(pilot)
        assert current(app) == ".tmplfile"  # same position


async def test_the_current_row_clamps(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("j", "j", "j", "j", "j")
        assert current(app) == "hello.sh"
        fake.status = status_without("hello.sh", ".tool", ".tmplfile")
        await pilot.press("g")
        await settled(pilot)
        assert current(app) == ".mine"


# --- Key bar -----------------------------------------------------------------------------


async def test_key_bar_with_rows(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#key-bar") == "space Select  / Filter  ? Help"


async def test_key_bar_without_rows(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#key-bar") == "? Help"
        await pilot.press("slash", "space")
        assert not app.query_one(FilterInput).display


async def test_key_bar_after_a_failure(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.fail("status", b"chezmoi: broke\n", binary="chezmoi")
        await pilot.press("g")
        await settled(pilot)
        assert text(app, "#key-bar") == "? Help"
