"""The Drift view's rows and detail (spec §6 Drift, *Rows and detail in the TUI*; DEC-34).

Every test but the one marked `requires_chezmoi` runs on `FakeChezmoi`, whose
diffs are the ones chezmoi recorded (tests/fixtures/chezmoi/diff.json).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from textual.content import Content
from textual.widgets import Static

from cheztui.config import Config, SecretsConfig, UiConfig
from cheztui.ops import DriftRow
from cheztui.runner import Completed
from cheztui.secrets import MASK
from cheztui.tui.app import CheztuiApp
from cheztui.tui.views.base import RowList
from cheztui.tui.views.drift import DriftView
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_chezmoi import FakeChezmoi
from tests.fake_write import subcommand
from tests.test_chezmoi_reads import HOME, ROOT, build_drift
from tests.test_tui import TOKEN, make_app, settled, text
from tests.test_tui_screens import palette_entries


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(HOME))
    return HOME


def view(app: CheztuiApp) -> DriftView:
    return app.query_one(DriftView)


def prompts(app: CheztuiApp) -> list[Content]:
    listing = app.query_one(RowList)
    contents = []
    for i in range(listing.option_count):
        prompt = listing.get_option_at_index(i).prompt
        assert isinstance(prompt, Content)
        contents.append(prompt)
    return contents


def spans(content: Content) -> list[tuple[str, str]]:
    """Each styled stretch of `content` with its style."""
    return [(content.plain[s.start : s.end], str(s.style)) for s in content.spans]


def detail(app: CheztuiApp, part: str = "body") -> Content:
    content = app.query_one(f"#detail-{part}", Static).render()
    assert isinstance(content, Content)
    return content


def body(app: CheztuiApp) -> str:
    return text(app, "#detail-body")


def head(app: CheztuiApp) -> str:
    return text(app, "#detail-head")


def calls(fake: FakeChezmoi, command: str) -> int:
    return sum(1 for c in fake.chezmoi_calls if c[:1] == (command,))


async def move_to(pilot, name: str) -> None:
    app = pilot.app
    names = [r.target.name for r in view(app).shown]
    index = app.query_one(RowList).highlighted or 0
    step = names.index(name) - index
    await pilot.press(*(["j"] if step > 0 else ["k"]) * abs(step))
    await settled(pilot)


def key_bar(app: CheztuiApp) -> str:
    return text(app, "#key-bar")


def with_token(fake: FakeChezmoi, name: str = ".tool") -> None:
    """The row's recorded diff with a token on its added line."""
    target = HOME / name
    fake.diff[target] = fake.diff[target].replace("+echo more", f"+echo {TOKEN}")


# --- Rows (spec §6 Row) ----------------------------------------------------------------


async def test_rows_show_direction_columns_target_and_tags(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        rows = prompts(app)
        assert [r.plain for r in rows] == [
            "  both MM  ~/.both file",
            "  you→ DA  ~/.gone",
            "  you→ MM  ~/.mine",
            "  src→  M  ~/.tmplfile  tmpl",
            "  src→  M  ~/.tool  exec",
            "  run   R  ~/hello.sh",
        ]
        assert spans(rows[0]) == [("both", "$error")]
        assert spans(rows[1]) == [("you→", "$warning")]
        assert spans(rows[3]) == [("src→", "$success"), ("tmpl", "$text-muted")]
        assert spans(rows[4]) == [("src→", "$success"), ("exec", "$text-muted")]
        assert spans(rows[5]) == [("run ", "$accent")]


def drift_row(**kwargs) -> DriftRow:
    row = DriftRow(
        target=HOME / "x",
        source=ROOT / "source" / "x",
        direction="src",
        default_fix="apply",
        columns=" M",
        tags=(),
        kind="file",
        fix_overwrites=False,
    )
    return replace(row, **kwargs)


async def test_every_tag_in_order(tmp_path):
    # No recorded row is a directory or a symlink, so this row is built.
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        row = drift_row(tags=("tmpl", "enc", "exec", "link", "ext", "dir"))
        content = view(app).content(row)
        assert content.plain == "src→  M  ~/x  tmpl enc exec link ext dir"
        assert spans(content)[-1] == ("tmpl enc exec link ext dir", "$text-muted")


async def test_the_filter_matches_the_target_alone(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await pilot.press("slash", *"run", "enter")
        assert view(app).shown == ()
        await pilot.press("slash", "ctrl+u", *"tmpl", "enter")
        assert [r.target.name for r in view(app).shown] == [".tmplfile"]  # its name, not its tag


# --- The head (spec §6 Detail head) -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "first", "source", "fix"),
    [
        (".both file", "~/.both file  both sides changed", "dot_both file", "merge"),
        (".gone", "~/.gone  the target was deleted", "dot_gone", "apply"),
        (".mine", "~/.mine  you changed the target", "dot_mine", "re-add"),
        (".tool", "~/.tool  the source changed", "executable_dot_tool", "apply"),
        ("hello.sh", "~/hello.sh  the script will run", "run_onchange_hello.sh", "apply"),
    ],
)
async def test_head_says_the_direction_in_words(tmp_path, name, first, source, fix):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, name)
        assert head(app) == f"{first}\nsource  /scratch/source/{source}\nfix     {fix}"


async def test_head_without_a_source(tmp_path):
    fake = FakeChezmoi.drifted(status=" M /scratch/home/.unknown\n")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert head(app) == "~/.unknown  the source changed\nsource  (none)\nfix     apply"


async def test_head_says_when_the_fix_overwrites(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        row = drift_row(direction="you", kind="dir", columns="MM", fix_overwrites=True)
        assert view(app).head(row).plain == (
            "~/x  you changed the target\nsource  /scratch/source/x\n"
            "fix     apply (overwrites your change)"
        )
        assert view(app).head(drift_row()).plain.endswith("\nfix     apply")


async def test_no_row_no_detail(tmp_path):
    app = make_app(FakeChezmoi.in_sync(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert (head(app), body(app)) == ("", "")
        assert not app.query_one("#detail-rule").display


# --- The body: the diff (spec §6 Body: the diff) -------------------------------------------


async def test_the_diff_is_coloured_by_line_kind(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert app.query_one("#detail-rule").display
        content = detail(app)
        assert content.plain == FakeChezmoi.drifted().diff[HOME / ".both file"].rstrip("\n")
        assert spans(content) == [
            ("diff --git a/.both file b/.both file", "bold"),
            (
                "index 08ea899571c402193ffb4fad937572e7dd5483ec"
                "..08b5b10ff7ef72ddb064bdd18bd91f57f034b19e 100644",
                "bold",
            ),
            ("--- a/.both file", "bold"),
            ("+++ b/.both file", "bold"),
            ("@@ -1,2 +1,2 @@", "$accent"),
            ("-edited in home", "$error"),
            ("+edited in source", "$success"),
        ]  # the context line ` both` is plain


async def test_the_diff_call(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        diffs = [c for c in fake.chezmoi_calls if c[:1] == ("diff",)]
        assert diffs == [
            ("diff", "--no-pager", "--color=false", "--use-builtin-diff", str(HOME / ".both file"))
        ]


async def test_empty_output_is_no_diff(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.diff[HOME / ".both file"] = ""
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert body(app) == "(no diff)"


async def test_diff_context_cuts_the_body(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path, ui=UiConfig(diff_context=0))
    async with app.run_test() as pilot:
        await settled(pilot)
        assert body(app).endswith("@@ -2 +2 @@\n-edited in home\n+edited in source")


async def test_diff_context_is_read_when_the_body_is(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert " both\n" in body(app)
        app.settings.set("ui.diff_context", 0)
        await pilot.press("g")
        await settled(pilot)
        assert " both\n" not in body(app)


# --- Detail reads (spec §6 Detail reads) ----------------------------------------------------


async def test_reading_while_the_diff_runs(tmp_path):
    fake = FakeChezmoi.drifted()
    release = fake.hold("diff")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        for _ in range(100):
            if view(app).current() is not None:
                break
            await pilot.pause()
        await pilot.pause()
        assert head(app).startswith("~/.both file  ")
        assert body(app) == "reading…"
        release.set()
        await settled(pilot)
        assert body(app).startswith("diff --git a/.both file")


async def test_moving_away_cancels_the_read_silently(tmp_path):
    fake = FakeChezmoi.drifted()
    release = fake.hold("diff")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        for _ in range(100):
            if body(app) == "reading…":
                break
            await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        workers = [w for w in app.workers if w.group == "body" and not w.is_finished]
        assert len(workers) == 1  # the first row's read is gone; `.gone`'s is held
        assert head(app).startswith("~/.gone  ")
        release.set()
        await settled(pilot)
        assert app.session_log == []
        assert body(app).startswith("diff --git a/.gone")
        assert (HOME / ".both file", False) not in view(app).bodies


async def test_moving_to_a_kept_body_cancels_the_read(tmp_path):
    # No new read starts to cancel the held one: the move itself does.
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("diff")
        await pilot.press("j")
        await pilot.pause()
        assert body(app) == "reading…"
        await pilot.press("k")
        await pilot.pause()
        assert [w for w in app.workers if w.group == "body" and not w.is_finished] == []
        assert body(app).startswith("diff --git a/.both file")
        release.set()
        await settled(pilot)
        assert (HOME / ".gone", False) not in view(app).bodies
        assert app.session_log == []


async def test_moving_back_shows_the_kept_body(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        first = body(app)
        await move_to(pilot, ".gone")
        assert calls(fake, "diff") == 2
        await move_to(pilot, ".both file")
        assert body(app) == first
        assert calls(fake, "diff") == 2


async def test_g_reads_the_bodies_again(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".gone")
        await pilot.press("g")
        await settled(pilot)
        assert calls(fake, "diff") == 3
        await move_to(pilot, ".both file")
        assert calls(fake, "diff") == 4  # its kept body went with the refresh


def encrypt(fake: FakeChezmoi, name: str) -> None:
    """Make `managed --include=encrypted` list the target, as `chezmoi add --encrypt` would."""
    entries = json.loads(fake.managed[None])
    fake.managed["encrypted"] = json.dumps({name: entries[name]})


async def until_kept(pilot, name: str) -> None:
    """Wait for the row's body read to finish, whatever else is held."""
    for _ in range(200):
        if (HOME / name, False) in view(pilot.app).bodies:
            return
        await pilot.pause()
    raise AssertionError(f"no body kept for {name}")


@pytest.mark.parametrize("hold_status", [False, True])
async def test_a_refreshed_row_is_read_for_its_new_value(tmp_path, hold_status):
    # A body is masked for the row on screen: a refresh that tags it `enc` reads it
    # again, whole-masked, even when the old row's read finished first (DEC-34).
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        assert "+echo more" in body(app)
        encrypt(fake, ".tool")
        if hold_status:
            release = fake.hold("status")
            await pilot.press("g")
            await until_kept(pilot, ".tool")  # read with the old row, during the refresh
            release.set()
        else:
            await pilot.press("g")
        await settled(pilot)
        row = view(app).current()
        assert row is not None
        assert row.tags == ("enc", "exec")
        assert body(app).endswith(f"@@ -1,2 +1,3 @@\n {MASK}\n {MASK}\n+{MASK}")
        kept = [v for k, v in view(app).bodies.items() if k[0] == HOME / ".tool"]
        assert len(kept) == 1
        assert kept[0][0] == row
        assert kept[0][1].masked
        assert "echo more" not in repr(view(app).bodies)


@pytest.mark.parametrize("hold_status", [False, True])
async def test_an_unchanged_row_keeps_the_refresh_read(tmp_path, hold_status):
    # The body `g` starts is kept when the refreshed row is the same value.
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        before = calls(fake, "diff")
        if hold_status:
            release = fake.hold("status")
            await pilot.press("g")
            await until_kept(pilot, ".tool")
            release.set()
        else:
            await pilot.press("g")
        await settled(pilot)
        assert calls(fake, "diff") == before + 1
        assert "+echo more" in body(app)


async def test_a_cancelled_refresh_still_reads_the_body(tmp_path):
    # `esc` keeps the rows on screen, and their body is read again for them.
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        before = calls(fake, "diff")
        release = fake.hold("status")
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("escape")
        await until_kept(pilot, ".tool")
        release.set()
        await settled(pilot)
        assert calls(fake, "diff") == before + 1
        assert "+echo more" in body(app)


async def test_the_destination_is_read_once_per_refresh(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        before = calls(fake, "dump-config")  # the header's and the bodies'
        for name in (".gone", ".mine", ".tmplfile", ".tool"):
            await move_to(pilot, name)
        assert calls(fake, "dump-config") == before
        await pilot.press("g")
        await settled(pilot)
        await move_to(pilot, ".both file")
        assert calls(fake, "dump-config") == before + 2


async def test_the_destination_read_survives_a_move(tmp_path):
    fake = FakeChezmoi.drifted()
    release = fake.hold("dump-config")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        for _ in range(100):
            if body(app) == "reading…":
                break
            await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        release.set()
        await settled(pilot)
        assert calls(fake, "dump-config") == 2  # the header's, and one for the bodies
        assert body(app).startswith("diff --git a/.gone")


async def test_a_failed_read_shows_and_logs_its_masked_error(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.fail("diff", f"chezmoi: first\nchezmoi: token {TOKEN}\n".encode(), binary="chezmoi")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert body(app) == f"✗ chezmoi exited 1: chezmoi: token {MASK}"
        assert spans(detail(app)) == [(body(app), "$error")]
        assert [(e.text, e.diagnostic) for e in app.session_log] == [
            (
                f"✗ chezmoi exited 1: chezmoi: token {MASK}",
                f"chezmoi: first\nchezmoi: token {MASK}",
            )
        ]
        assert text(app, "#log-line") == ""  # left alone
        await move_to(pilot, ".gone")
        assert len(app.session_log) == 2  # each shown failure is logged as it's shown
        assert TOKEN not in repr(app.session_log)


async def test_a_failure_isnt_kept(tmp_path):
    # Moving back reads the row again, and a second failure is logged again (#38).
    fake = FakeChezmoi.drifted()
    target = str(HOME / ".both file")

    def fail_both(argv, env):
        if argv[0] == "chezmoi" and subcommand(argv)[:1] == ("diff",) and argv[-1] == target:
            return Completed(argv, 1, b"", b"chezmoi: locked\n")
        return None

    fake.reply(fail_both)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert body(app) == "✗ chezmoi exited 1: chezmoi: locked"
        await move_to(pilot, ".gone")
        await move_to(pilot, ".both file")
        assert body(app) == "✗ chezmoi exited 1: chezmoi: locked"
        assert sum(1 for c in fake.chezmoi_calls if c[:1] == ("diff",) and c[-1] == target) == 2
        assert [e.text for e in app.session_log] == ["✗ chezmoi exited 1: chezmoi: locked"] * 2


async def test_a_failure_is_logged_when_shown_not_when_the_refresh_ends(tmp_path):
    # The header's read is held while the body read fails: the failure is in
    # the session log before the refresh ends.
    fake = FakeChezmoi.drifted()
    fake.fail("diff", b"chezmoi: diff broke\n", binary="chezmoi")
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        release = fake.hold("symbolic-ref")
        await pilot.press("g")
        for _ in range(100):
            if len(app.session_log) == 2:
                break
            await pilot.pause()
        assert app.reading
        assert body(app) == "✗ chezmoi exited 1: chezmoi: diff broke"
        assert app.session_log[-1].text == "✗ chezmoi exited 1: chezmoi: diff broke"
        release.set()
        await settled(pilot)
        assert len(app.session_log) == 2


async def test_a_failed_destination_read_is_a_failed_body(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        fake.fail("dump-config", f"chezmoi: {TOKEN}\n".encode(), binary="chezmoi")
        await pilot.press("g")
        await settled(pilot)
        assert body(app) == f"✗ chezmoi exited 1: chezmoi: {MASK}"


# --- `t` (spec §6 Body: rendered) -------------------------------------------------------------


async def test_t_is_only_for_templates(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert not key_bar(app).startswith("t ")
        assert not any(e.startswith("Rendered") for e in await palette_entries(pilot))
        await pilot.press("escape")
        await pilot.press("t")
        await settled(pilot)
        assert not view(app).rendered


async def test_t_toggles_the_rendered_output(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        assert key_bar(app).startswith("t Rendered  ")
        assert body(app).endswith("@@ -1 +1,2 @@\n TMPL\n+MORE")
        await pilot.press("t")
        await settled(pilot)
        assert body(app) == "TMPL\nMORE"
        assert spans(detail(app)) == []  # plain
        assert key_bar(app).startswith("t Diff  ")
        assert ("cat", str(HOME / ".tmplfile")) in fake.chezmoi_calls
        await pilot.press("t")
        await settled(pilot)
        assert body(app).endswith("+MORE")
        assert key_bar(app).startswith("t Rendered  ")


async def test_the_palette_names_t_by_what_is_showing(tmp_path):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        entries = await palette_entries(pilot)
        assert entries[0] == "Rendered  t"
        await pilot.press(*"rendered", "enter")
        await settled(pilot)
        assert view(app).rendered
        assert body(app) == "TMPL\nMORE"


@pytest.mark.parametrize("leave", ["move", "refresh"])
async def test_a_move_or_a_refresh_returns_to_the_diff(tmp_path, leave):
    app = make_app(FakeChezmoi.drifted(), tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        await pilot.press("t")
        await settled(pilot)
        if leave == "move":
            await move_to(pilot, ".tool")
            await move_to(pilot, ".tmplfile")
        else:
            await pilot.press("g")
            await settled(pilot)
        assert not view(app).rendered
        assert body(app).endswith("+MORE")
        assert key_bar(app).startswith("t Rendered  ")


async def test_the_rendered_body_is_masked_as_a_body(tmp_path):
    fake = FakeChezmoi.drifted()
    fake.cat[HOME / ".tmplfile"] = f"TMPL\ntoken = {TOKEN}\n".encode()
    app = make_app(fake, tmp_path, ui=UiConfig(diff_context=0))
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        await pilot.press("t")
        await settled(pilot)
        assert body(app) == f"TMPL\ntoken = {MASK}"  # and not cut


# --- `R` (spec §6 Reveal) -----------------------------------------------------------------------


async def test_r_needs_something_masked(tmp_path):
    fake = FakeChezmoi.drifted()
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert not app.available("reveal")
        assert not any(e.startswith("Reveal") for e in await palette_entries(pilot))
        await pilot.press("escape", "R")
        await settled(pilot)
        assert calls(fake, "diff") == 1
        assert "revealed" not in head(app)


async def test_r_reveals_and_masks_again(tmp_path):
    fake = FakeChezmoi.drifted()
    with_token(fake)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        assert f"+echo {MASK}" in body(app)
        assert "R " not in key_bar(app)  # never on the key bar
        assert any(e.startswith("Reveal") for e in await palette_entries(pilot))
        await pilot.press("escape", "R")
        await settled(pilot)
        assert f"+echo {TOKEN}" in body(app)
        assert head(app).endswith("fix     apply  revealed")
        assert spans(detail(app, "head")) == [("revealed", "$warning")]
        assert "R " not in key_bar(app)
        await pilot.press("R")
        await settled(pilot)
        assert f"+echo {MASK}" in body(app)
        assert head(app).endswith("fix     apply")
        assert TOKEN not in repr(view(app).bodies) + repr(app.session_log)


@pytest.mark.parametrize("leave", ["move", "refresh", "t"])
async def test_leaving_drops_the_revealed_text(tmp_path, leave):
    fake = FakeChezmoi.drifted()
    diff = fake.diff[HOME / ".tmplfile"].replace("@@ -1 +1,2 @@", "@@ -1 +1,3 @@")
    fake.diff[HOME / ".tmplfile"] = diff + f"+token = {TOKEN}\n"
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        await pilot.press("R")
        await settled(pilot)
        assert TOKEN in body(app)
        if leave == "move":
            await move_to(pilot, ".tool")
            await move_to(pilot, ".tmplfile")
        elif leave == "refresh":
            await pilot.press("g")
            await settled(pilot)
        else:
            await pilot.press("t", "t")
            await settled(pilot)
        assert TOKEN not in body(app)
        assert f"+token = {MASK}" in body(app)
        assert "revealed" not in head(app)
        assert TOKEN not in repr(view(app).bodies) + repr(app.session_log)


async def test_a_revealed_body_is_still_cut(tmp_path):
    fake = FakeChezmoi.drifted()
    with_token(fake)
    app = make_app(fake, tmp_path, ui=UiConfig(diff_context=0))
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        await pilot.press("R")
        await settled(pilot)
        assert body(app).endswith(f"@@ -2,0 +3 @@\n+echo {TOKEN}")


async def test_a_failed_reveal_keeps_its_diagnostic_masked(tmp_path):
    fake = FakeChezmoi.drifted()
    with_token(fake)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".tool")
        fake.fail("diff", f"chezmoi: {TOKEN}\n".encode(), binary="chezmoi")
        await pilot.press("R")
        await settled(pilot)
        assert body(app) == f"✗ chezmoi exited 1: chezmoi: {MASK}"
        assert TOKEN not in repr(app.session_log)
        reads = calls(fake, "diff")
        await pilot.press("R")  # back to the kept, masked body, with no new read
        await settled(pilot)
        assert f"+echo {MASK}" in body(app)
        assert calls(fake, "diff") == reads


async def test_the_whole_masked_body_reveals(tmp_path):
    # always_mask hides the whole body; R shows it (spec §5 Secrets, DEC-26).
    fake = FakeChezmoi.drifted()
    config = Config(
        tmp_path / "config.toml", secrets=replace(SecretsConfig(), always_mask=("~/.mine",))
    )
    app = CheztuiApp(config, fake.chezmoi(), git_runner=fake.git_runner)
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        assert f" {MASK}\n-{MASK}" in body(app)
        await pilot.press("R")
        await settled(pilot)
        assert " mine\n-edited in home" in body(app)


async def test_mask_globs_use_chezmoi_destination(tmp_path, monkeypatch):
    # `~/` in always_mask is the destination dump-config reports, not $HOME (DEC-26).
    monkeypatch.setenv("HOME", "/elsewhere")
    fake = FakeChezmoi.drifted()
    config = Config(
        tmp_path / "config.toml", secrets=replace(SecretsConfig(), always_mask=("~/.both file",))
    )
    app = CheztuiApp(config, fake.chezmoi(), git_runner=fake.git_runner)
    async with app.run_test() as pilot:
        await settled(pilot)
        assert body(app).endswith(f"@@ -1,2 +1,2 @@\n {MASK}\n-{MASK}\n+{MASK}")


# --- Live -----------------------------------------------------------------------------------


@requires_chezmoi
async def test_live_detail(scratch: Scratch, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(scratch.home))
    build_drift(scratch)
    app = CheztuiApp(Config(scratch.root / "cheztui.toml"), scratch.chezmoi())
    async with app.run_test() as pilot:
        await settled(pilot)
        await move_to(pilot, ".mine")
        assert head(app).startswith("~/.mine  you changed the target\n")
        assert body(app).startswith("diff --git a/.mine b/.mine\n")
        assert "\n-edited in home" in body(app)


# --- Snapshot -------------------------------------------------------------------------------


def test_snapshot_rendered(snap_compare, tmp_path):
    fake = FakeChezmoi.drifted(upstream=set(), counts=(1, 2), stray=["a", "b"])

    async def rendered(pilot) -> None:
        await settled(pilot)
        await move_to(pilot, ".tmplfile")
        await pilot.press("t")
        await settled(pilot)

    assert snap_compare(make_app(fake, tmp_path), terminal_size=(120, 32), run_before=rendered)
