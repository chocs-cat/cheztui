"""The preview modal (spec §6 Drift, *The preview modal*; DEC-4, DEC-16, DEC-35).

Every plan comes from `WriteOps.prepare*` on `FakeChezmoi.drifted(root)`, whose
previews are the ones chezmoi recorded (tests/fixtures/chezmoi/preview.json)
and whose home has real files, so eligibility sees what chezmoi left.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Static

from cheztui import ops
from cheztui.config import Config, UiConfig
from cheztui.ops import DriftRow, WritePlan
from cheztui.secrets import MASK
from cheztui.tui.app import CheztuiApp
from cheztui.tui.widgets.confirm import PreviewModal
from tests.fake_chezmoi import FakeChezmoi
from tests.test_tui import TOKEN, make_app, settled

# The snapshots show the commands' absolute targets, so their root is fixed.
SNAPSHOT_ROOT = Path("/tmp/cheztui-snapshots")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "scratch" / "home"))
    return tmp_path / "scratch"


@pytest.fixture
def snapshot_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    shutil.rmtree(SNAPSHOT_ROOT, ignore_errors=True)
    monkeypatch.setenv("HOME", str(SNAPSHOT_ROOT / "home"))
    yield SNAPSHOT_ROOT
    shutil.rmtree(SNAPSHOT_ROOT, ignore_errors=True)


async def drift_rows(app: CheztuiApp) -> dict[str, DriftRow]:
    return {r.target.name: r for r in (await ops.drift(app.chezmoi)).rows}


def writes(app: CheztuiApp, fake: FakeChezmoi) -> ops.WriteOps:
    return ops.WriteOps(app.chezmoi, app.settings, git_runner=fake.git_runner)


async def show(pilot: Pilot, plan: WritePlan, *, context: int = 3) -> list[WritePlan | None]:
    """Open the modal over the app; the list gets what it's dismissed with."""
    results: list[WritePlan | None] = []
    pilot.app.push_screen(PreviewModal(plan, context=context), results.append)
    await pilot.pause()
    return results


def static(app: CheztuiApp, selector: str) -> Static:
    return app.screen.query_one(selector, Static)


def part(app: CheztuiApp, selector: str) -> str:
    return str(static(app, selector).render())


def entries(app: CheztuiApp) -> list[str]:
    """The entries section's lines, headings included, the current one marked `>`."""
    lines = []
    for line in app.screen.query("#preview-entries Static").results(Static):
        mark = ">" if line.has_class("-current") else " "
        lines.append(f"{mark}{line.render()}")
    return lines


def diff(app: CheztuiApp) -> str:
    return part(app, "#preview-diff") if static(app, "#preview-diff").display else ""


def footer(app: CheztuiApp) -> str:
    return part(app, "#preview-footer")


def spans(app: CheztuiApp, selector: str) -> list[tuple[str, str]]:
    content = static(app, selector).render()
    assert isinstance(content, Content)
    return [(content.plain[s.start : s.end], str(s.style)) for s in content.spans]


def command(fake: FakeChezmoi, *words: str) -> str:
    return "$ " + " ".join(["chezmoi", "--no-tty", *words])


async def test_two_actions_selected_together(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    home = root / "home"
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare_defaults([rows[".mine"], rows[".tool"]])
        await show(pilot, plan)
        assert part(app, "#preview-commands") == "\n".join(
            [
                command(fake, "apply", "--force", "--recursive=false", str(home / ".tool")),
                command(fake, "re-add", "--recursive=false", str(home / ".mine")),
            ]
        )
        assert entries(app) == [" apply", ">  src→ ~/.tool", " re-add", "   you→ ~/.mine"]
        assert diff(app) == fake.previews["apply"][home / ".tool"].removesuffix("\n")
        assert ("+echo more", "$success") in spans(app, "#preview-diff")
        await pilot.press("k")  # at the top: stays
        assert entries(app)[1].startswith(">")
        await pilot.press("j")
        assert entries(app)[3].startswith(">")
        assert diff(app) == fake.previews["re-add"][home / ".mine"].removesuffix("\n")
        await pilot.press("j")  # at the bottom: stays
        assert entries(app)[3].startswith(">")
        assert footer(app) == "enter Run  j/k Move  esc Cancel"


@pytest.mark.parametrize(("key", "returns_plan"), [("enter", True), ("escape", False)])
async def test_enter_returns_the_plan_and_esc_none(tmp_path, root, key, returns_plan):
    fake = FakeChezmoi.drifted(root)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("re-add", [rows[".mine"]])
        results = await show(pilot, plan)
        await pilot.press(key)
        await pilot.pause()
        assert results == [plan if returns_plan else None]
        assert not isinstance(app.screen, PreviewModal)
    assert [w for w in fake.writes if "--dry-run" not in w] == []  # the modal runs nothing


async def test_apply_all_ticks_and_rebuilds(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    home = root / "home"
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare_apply_all(list(rows.values()))
        results = await show(pilot, plan)
        assert entries(app) == [
            " apply",
            ">  [ ] both ~/.both file overwrites your change",
            "   [ ] you→ ~/.gone overwrites your change",
            "   [ ] you→ ~/.mine overwrites your change",
            "   [x] src→ ~/.tmplfile",
            "   [x] src→ ~/.tool",
            "   [x] run  ~/hello.sh",
        ]
        assert ("overwrites your change", "$warning") in spans(app, "#entry-0")
        assert footer(app) == "enter Run  space Tick  j/k Move  esc Cancel"
        await pilot.press("j", "j", "space")
        assert entries(app)[3] == ">  [x] you→ ~/.mine overwrites your change"
        assert str(home / ".mine") in part(app, "#preview-commands")
        assert str(home / ".gone") not in part(app, "#preview-commands")
        # Untick everything: nothing ticked, and enter does nothing.
        await pilot.press("space", "j", "space", "j", "space", "j", "space")
        assert part(app, "#preview-commands") == ""
        assert footer(app) == "nothing ticked  space Tick  j/k Move  esc Cancel"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PreviewModal)
        await pilot.press("k", "space", "enter")  # re-tick `.tool`, then run
        await pilot.pause()
        (chosen,) = results
        assert chosen is not None
        assert [c.targets for c in chosen.commands] == [(home / ".tool",)]


async def test_a_mixed_selection_left_out_rows_and_directory_descendants(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    home = root / "home"
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        mixed = await writes(app, fake).prepare_defaults([rows[".both file"], rows[".tool"]])
        await show(pilot, mixed)
        assert entries(app) == [
            " apply",
            ">  src→ ~/.tool",
            " then merge, suspended:",
            "   both ~/.both file",
        ]
        await pilot.press("j")
        assert diff(app) == "(no preview)"
        await pilot.press("escape")

        left_out = await writes(app, fake).prepare("re-add", [rows[".mine"], rows[".tmplfile"]])
        await show(pilot, left_out)
        assert entries(app)[2:] == ["   src→ ~/.tmplfile left out: a template"]
        assert ("left out: a template", "$text-muted") in spans(app, "#entry-1")
        await pilot.press("escape")

        directory = DriftRow(
            target=home / ".dir",
            source=root / "source" / "dot_dir",
            direction="src",
            default_fix="apply",
            columns=" M",
            tags=("dir",),
            kind="dir",
            fix_overwrites=False,
        )
        forget = await writes(app, fake).prepare("forget", [directory])
        await show(pilot, forget)
        assert entries(app) == [" forget", ">  src→ ~/.dir", "        ~/.dir/inner with ~/.dir"]
        assert diff(app) == "(no diff)"  # its dry run printed nothing (DEC-36)
        assert part(app, "#preview-commands") == command(
            fake, "forget", "--force", str(home / ".dir")
        )


async def test_stray_files_warn_with_auto_commit_off(tmp_path, root):
    fake = FakeChezmoi.drifted(root, stray=["dot_stray"])
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("re-add", [rows[".mine"]])
        await show(pilot, plan)
        assert part(app, "#preview-stray") == (
            "warning: uncommitted files in the source tree:\n  dot_stray"
        )
        assert diff(app)  # the diff follows the warning
        assert footer(app) == "enter Run  j/k Move  esc Cancel"


async def test_a_blocked_plan_only_closes(tmp_path, root):
    fake = FakeChezmoi.drifted(root, stray=["dot_stray"], auto_commit=True)
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("re-add", [rows[".mine"]])
        results = await show(pilot, plan)
        assert part(app, "#preview-stray") == (
            "✗ the source tree has uncommitted changes; commit or remove them first:\n  dot_stray"
        )
        assert spans(app, "#preview-stray")[0][1] == "$error"
        assert diff(app) == ""
        assert footer(app) == "esc Close"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PreviewModal)
        await pilot.press("escape")
        await pilot.pause()
        assert results == [None]
    assert not [c for c in fake.chezmoi_calls if "--dry-run" in c]
    assert fake.writes == []


async def test_a_failed_preview_shows_its_masked_error(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    fake.fail("re-add", f"chezmoi: {TOKEN}\n".encode(), binary="chezmoi")
    app = make_app(fake, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("re-add", [rows[".mine"]])
        await show(pilot, plan)
        assert part(app, "#preview-stray") == f"✗ preview failed: chezmoi: {MASK}"
        assert footer(app) == "esc Close"
        assert diff(app) == ""


async def test_dry_run_mode_says_dry_run(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    app = make_app(fake, tmp_path, dry_run=True)
    async with app.run_test() as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("apply", [rows[".tool"]])
        await show(pilot, plan)
        assert footer(app) == "enter Dry run  j/k Move  esc Cancel"


async def test_diff_context_cuts_the_diff(tmp_path, root):
    fake = FakeChezmoi.drifted(root)
    app = make_app(fake, tmp_path)
    async with app.run_test() as pilot:
        await settled(pilot)
        rows = await drift_rows(app)
        plan = await writes(app, fake).prepare("apply", [rows[".tool"]])
        await show(pilot, plan, context=1)
        assert diff(app).endswith("@@ -2 +2,2 @@\n echo tool\n+echo more")


# --- Snapshots (spec §11 TUI) -----------------------------------------------------------


def snapshot_app(root: Path, **fake_kwargs) -> tuple[CheztuiApp, FakeChezmoi]:
    fake = FakeChezmoi.drifted(root, **fake_kwargs)
    config = Config(root / "config.toml", ui=UiConfig())
    return CheztuiApp(config, fake.chezmoi(), git_runner=fake.git_runner), fake


def opener(fake: FakeChezmoi, prepare):
    async def run_before(pilot: Pilot) -> None:
        await settled(pilot)
        app = pilot.app
        assert isinstance(app, CheztuiApp)
        plan = await prepare(writes(app, fake), await drift_rows(app))
        await show(pilot, plan)

    return run_before


def test_snapshot_two_entries(snap_compare, snapshot_root):
    app, fake = snapshot_app(snapshot_root)

    async def prepare(w, rows):
        return await w.prepare_defaults([rows[".mine"], rows[".tool"]])

    assert snap_compare(app, terminal_size=(120, 32), run_before=opener(fake, prepare))


def test_snapshot_apply_all(snap_compare, snapshot_root):
    app, fake = snapshot_app(snapshot_root)

    async def prepare(w, rows):
        return await w.prepare_apply_all(list(rows.values()))

    assert snap_compare(app, terminal_size=(120, 32), run_before=opener(fake, prepare))


def test_snapshot_blocked(snap_compare, snapshot_root):
    app, fake = snapshot_app(snapshot_root, stray=["dot_stray"], auto_commit=True)

    async def prepare(w, rows):
        return await w.prepare("re-add", [rows[".mine"]])

    assert snap_compare(app, terminal_size=(120, 32), run_before=opener(fake, prepare))
