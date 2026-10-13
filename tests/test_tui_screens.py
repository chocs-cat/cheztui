"""Help, the palette, the session log and settings (spec §5 Help, palette, session log; §9).

Settings tests use a config file under `tmp_path`; nothing reads or writes the
real one.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from textual.command import CommandPalette
from textual.widgets import Input, Static

from cheztui.config import Config, load
from cheztui.tui import keys
from cheztui.tui.app import CheztuiApp
from cheztui.tui.screens import HelpScreen, Palette, SessionLogScreen
from cheztui.tui.settings import SettingsScreen, ThemePicker
from tests.fake_chezmoi import FakeChezmoi
from tests.test_chezmoi_reads import ROOT
from tests.test_tui import TOKEN, make_app, settled, text


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(ROOT / "home"))


def screen_text(app: CheztuiApp) -> str:
    return str(app.screen.query_one("#text", Static).render())


# --- Help ------------------------------------------------------------------------------

GLOBAL_ORDER = [
    "1",
    "j/k ↑/↓",
    "J/K PgUp/PgDn",
    "/",
    "space",
    "g",
    ":",
    "l",
    "R",
    ",",
    "?",
    "q",
]


async def test_help_lists_the_global_keys_in_the_tables_order(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        lines = screen_text(app).splitlines()
        # The view's section first, then a blank line, then the global keys.
        blank = lines.index("")
        assert lines[0] == "Drift"
        assert [line.split()[0] for line in lines[1:blank]] == [
            "enter",
            "a",
            "r",
            "m",
            "e",
            "d",
            "t",
            "M",
            "x",
            "A",
        ]
        assert lines[blank + 1] == "Global"
        lines = lines[blank + 2 :]
        shown = [line.split("  ")[0].strip() for line in lines]
        assert shown[: len(GLOBAL_ORDER)] == GLOBAL_ORDER
        # Every declared binding is there, with its description.
        for key in keys.GLOBAL:
            assert any(
                line.startswith(key.keys) and line.endswith(f"  {key.description}")
                for line in lines
            )


@pytest.mark.parametrize("close", ["escape", "question_mark", "q"])
async def test_help_closes(tmp_path, close):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("question_mark", close)
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running  # `q` closed help rather than quitting


async def test_frame_keys_do_nothing_under_help(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        calls = len(fake.log)
        await pilot.press("question_mark", "g", "slash", "j")
        await settled(pilot)
        assert len(fake.log) == calls
        assert app.query_one("#filter").display is False


def test_snapshot_help(snap_compare, tmp_path):
    async def open_help(pilot):
        await settled(pilot)
        await pilot.press("question_mark")
        await pilot.pause()

    app = make_app(FakeChezmoi.drifted(), tmp_path)
    assert snap_compare(app, terminal_size=(120, 32), run_before=open_help)


async def test_key_bar_ends_with_help(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#key-bar").endswith("  ? Help")


async def test_key_bar_in_sync_is_help_alone(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert text(app, "#key-bar") == "? Help"


# --- Palette ------------------------------------------------------------------------------


async def palette_entries(pilot) -> list[str]:
    app = pilot.app
    await pilot.press("colon")
    await pilot.pause()
    assert isinstance(app.screen, Palette)
    # With an empty query the palette lists what its provider discovers.
    for _ in range(20):
        await pilot.pause(0.05)
        options = app.screen.query_one("CommandList")
        if options.option_count:  # type: ignore[attr-defined]
            break
    options = app.screen.query_one("CommandList")
    return [
        str(options.get_option_at_index(i).prompt)  # type: ignore[attr-defined]
        for i in range(options.option_count)  # type: ignore[attr-defined]
    ]


async def test_palette_lists_what_works_on_drift(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        entries = await palette_entries(pilot)
        # The view's actions that work on `.both file` (targets missing here), then the frame's.
        assert entries == [
            "Fix  enter",
            "Apply  a",
            "Edit  e",
            "Full diff  d",
            "Forget  x",
            "Apply all  A",
            "Filter  /",
            "Select  space",
            "Refresh  g",
            "Session log  l",
            "Settings  ,",
            "Help  ?",
            "Quit  q",
        ]


async def test_palette_in_sync_has_no_filter_or_select(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        entries = await palette_entries(pilot)
        assert "Filter  /" not in entries
        assert "Select  space" not in entries
        assert "Refresh  g" in entries


async def test_choosing_session_log_opens_it(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await palette_entries(pilot)
        await pilot.press(*"session", "enter")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, SessionLogScreen)


async def test_choosing_quit_quits(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await palette_entries(pilot)
        await pilot.press(*"quit", "enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_ctrl_p_does_not_open_the_palette(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, CommandPalette)


# --- Session log ----------------------------------------------------------------------


async def test_session_log_entries(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.fail(
        "status",
        f"chezmoi: first line\nchezmoi: token {TOKEN}\n".encode(),
        binary="chezmoi",
    )
    app = make_app(fake, tmp_path)
    times = iter(datetime(2026, 10, 12, 9, 5, s) for s in (1, 7, 9))
    app.now = lambda: next(times)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("dump-config")
        await pilot.press("g")
        for _ in range(100):  # the reread's failure is logged as it's shown
            if len(app.session_log) == 2:
                break
            await pilot.pause()
        await pilot.press("escape")
        await settled(pilot)
        release.set()
        await pilot.press("l")
        assert isinstance(app.screen, SessionLogScreen)
        assert screen_text(app) == (
            "09:05:01  ✗ chezmoi exited 1: chezmoi: token ********\n"
            "  chezmoi: first line\n"
            "  chezmoi: token ********\n"
            "09:05:07  ✗ chezmoi exited 1: chezmoi: token ********\n"
            "  chezmoi: first line\n"
            "  chezmoi: token ********\n"
            "09:05:09  refresh cancelled"
        )


async def test_session_log_scrolls_to_the_newest(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    app.now = lambda: datetime(2026, 10, 12, 9, 0, 0)
    async with app.run_test(size=(80, 20)) as pilot:
        await settled(pilot)
        for i in range(60):
            app.record(f"entry {i}")
        await pilot.press("l")
        await pilot.pause()
        scroll = app.screen.query_one("VerticalScroll")
        assert scroll.scroll_y == scroll.max_scroll_y > 0


@pytest.mark.parametrize("close", ["escape", "l", "q"])
async def test_session_log_closes(tmp_path, close):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("l", close)
        assert not isinstance(app.screen, SessionLogScreen)
        assert app.is_running


# --- Settings ----------------------------------------------------------------------------


def settings_app(tmp_path: Path, text_: str | None = None, **flags) -> tuple[CheztuiApp, Path]:
    path = tmp_path / "cheztui" / "config.toml"
    if text_ is not None:
        path.parent.mkdir(parents=True)
        path.write_text(text_)
    fake = FakeChezmoi.drifted()
    app = CheztuiApp(load(path), fake.chezmoi(**flags), git_runner=fake.git_runner)
    return app, path


def row_text(app: CheztuiApp, key: str) -> str:
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    row = screen.row(key)
    parts = [str(s.render()) for s in row.query(Static)]
    return "  ".join(p for p in parts if p)


async def open_settings(pilot, key: str) -> None:
    await settled(pilot)
    await pilot.press("comma")
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, SettingsScreen)
    order = [
        "ui.theme",
        "ui.diff_context",
        "ui.narrow_breakpoint",
        "refresh.watch",
        "refresh.debounce_ms",
        "secrets.token_formats",
    ]
    for _ in range(order.index(key)):
        await pilot.press("down")


async def test_settings_lists_the_scalars_in_order(tmp_path):
    app, _ = settings_app(tmp_path)
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        rows = [
            row_text(app, k)
            for k in (
                "ui.theme",
                "ui.diff_context",
                "ui.narrow_breakpoint",
                "refresh.watch",
                "refresh.debounce_ms",
                "secrets.token_formats",
            )
        ]
        assert rows == [
            "ui.theme  textual-dark",
            "ui.diff_context  3",
            "ui.narrow_breakpoint  100",
            "refresh.watch  true",
            "refresh.debounce_ms  300",
            "secrets.token_formats  true",
        ]
        await pilot.press("escape")
        assert not isinstance(app.screen, SettingsScreen)


async def test_toggling_a_boolean_saves_at_once(tmp_path):
    app, path = settings_app(tmp_path)
    assert not path.exists()
    async with app.run_test() as pilot:
        await open_settings(pilot, "refresh.watch")
        await pilot.press("enter")
        await pilot.pause()
        assert row_text(app, "refresh.watch") == "refresh.watch  false"
        assert "watch = false" in path.read_text()
        assert app.settings.refresh.watch is False


async def test_an_invalid_integer_shows_its_error_and_isnt_saved(tmp_path):
    app, path = settings_app(tmp_path, "[ui]\nnarrow_breakpoint = 90\n")
    before = path.read_text()
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.narrow_breakpoint")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        await pilot.press("ctrl+u", "0", "enter")
        await pilot.pause()
        assert row_text(app, "ui.narrow_breakpoint") == (
            "ui.narrow_breakpoint  90  ✗ ui.narrow_breakpoint must be an integer ≥ 1"
        )
        assert path.read_text() == before
        assert app.settings.ui.narrow_breakpoint == 90


async def test_esc_in_an_integer_input_keeps_the_value(tmp_path):
    app, path = settings_app(tmp_path)
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.diff_context")
        await pilot.press("enter", "ctrl+u", "9", "escape")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert row_text(app, "ui.diff_context") == "ui.diff_context  3"
        assert not path.exists()


async def test_choosing_a_theme_applies_and_saves_it(tmp_path):
    app, path = settings_app(tmp_path)
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, ThemePicker)
        steps = picker.themes.index("nord") - picker.themes.index("textual-dark")
        await pilot.press(*["down" if steps > 0 else "up"] * abs(steps))
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "nord"
        assert 'theme = "nord"' in path.read_text()
        assert row_text(app, "ui.theme") == "ui.theme  nord"


async def test_a_theme_whose_save_fails_is_put_back(tmp_path):
    # The file is read-only: the row shows the error and the old theme, nothing
    # is applied, and a later change saves only its own key (spec §9, #32).
    app, path = settings_app(tmp_path, '[ui]\ntheme = "textual-dark"\n')
    before = path.read_text()
    path.chmod(0o444)
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, ThemePicker)
        steps = picker.themes.index("nord") - picker.themes.index("textual-dark")
        await pilot.press(*["down" if steps > 0 else "up"] * abs(steps))
        await pilot.press("enter")
        await pilot.pause()
        assert row_text(app, "ui.theme") == (
            "ui.theme  textual-dark  ✗ can't save: Permission denied"
        )
        assert app.theme == "textual-dark"
        assert app.settings.ui.theme == "textual-dark"
        assert path.read_text() == before
        path.chmod(0o644)
        await pilot.press("down", "down", "down", "down")
        await pilot.press("enter", "ctrl+u", *"500", "enter")
        await pilot.pause()
        assert path.read_text() == before + "\n[refresh]\ndebounce_ms = 500\n"
        assert app.theme == "textual-dark"


async def test_the_breakpoint_takes_effect_at_once(tmp_path):
    app, _ = settings_app(tmp_path)
    async with app.run_test(size=(120, 32)) as pilot:
        await open_settings(pilot, "ui.narrow_breakpoint")
        assert not app.narrow
        await pilot.press("enter", "ctrl+u", *"130", "enter")
        await pilot.pause()
        assert app.narrow
        assert app.settings.ui.narrow_breakpoint == 130


async def test_saving_keeps_comments(tmp_path):
    app, path = settings_app(tmp_path, "# mine\n[refresh]\nwatch = true  # keep me\n")
    async with app.run_test() as pilot:
        await open_settings(pilot, "refresh.debounce_ms")
        await pilot.press("enter", "ctrl+u", *"500", "enter")
        await pilot.pause()
        saved = path.read_text()
        assert saved.startswith("# mine\n")
        assert "# keep me" in saved
        assert "debounce_ms = 500" in saved


async def test_saves_happen_under_dry_run(tmp_path):
    app, path = settings_app(tmp_path, dry_run=True)
    async with app.run_test() as pilot:
        await open_settings(pilot, "secrets.token_formats")
        await pilot.press("enter")
        await pilot.pause()
        assert "token_formats = false" in path.read_text()


async def test_e_reloads_the_edited_file(tmp_path):
    app, path = settings_app(tmp_path)
    edited: list[Path] = []

    def editor(p: Path) -> None:
        edited.append(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('[ui]\ntheme = "nord"\ndiff_context = 7\n')

    app.launch_editor = editor
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        await pilot.press("e")
        await pilot.pause()
        assert edited == [path]
        assert app.settings.ui.diff_context == 7
        assert app.theme == "nord"
        assert row_text(app, "ui.diff_context") == "ui.diff_context  7"
        assert str(app.screen.query_one("#settings-error", Static).render()) == ""


async def test_e_with_an_invalid_file_keeps_the_old_settings(tmp_path):
    app, path = settings_app(tmp_path, "[ui]\ndiff_context = 5\n")
    app.launch_editor = lambda p: p.write_text("[invalid")
    async with app.run_test() as pilot:
        await open_settings(pilot, "ui.theme")
        await pilot.press("e")
        await pilot.pause()
        error = str(app.screen.query_one("#settings-error", Static).render())
        assert error.startswith(f"✗ {path}: not valid TOML")
        assert app.settings.ui.diff_context == 5
        assert isinstance(app.settings, Config)


def test_launch_editor_splits_the_command(monkeypatch, tmp_path):
    from cheztui.tui import settings

    runs: list[list[str]] = []
    monkeypatch.setattr(settings.subprocess, "run", lambda argv, check: runs.append(argv))
    monkeypatch.setenv("VISUAL", "code --wait")
    monkeypatch.setenv("EDITOR", "nano")
    settings.launch_editor(tmp_path / "c.toml")
    monkeypatch.delenv("VISUAL")
    settings.launch_editor(tmp_path / "c.toml")
    monkeypatch.delenv("EDITOR")
    settings.launch_editor(tmp_path / "c.toml")
    file = str(tmp_path / "c.toml")
    assert runs == [["code", "--wait", file], ["nano", file], ["vi", file]]
