"""The write path (spec §5 Write flow; DEC-8, DEC-23, DEC-27 to DEC-30).

Unit tests use `tests.fake_write`; the live tests run the real chezmoi and
git against the scratch setup, never with autoPush or a remote.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cheztui.chezmoi import Chezmoi, ChezmoiFailed, ChezmoiUnavailable
from cheztui.config import Config
from cheztui.git import GitFailed
from cheztui.ops import (
    Action,
    CommandResult,
    Direction,
    DriftRow,
    Fix,
    Preflight,
    WriteCommand,
    WriteOps,
    WritePlan,
    WriteResult,
    drift,
    eligibility,
    mask_diagnostic,
    short_error,
)
from cheztui.runner import Completed
from cheztui.secrets import MASK, Masker
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_write import FakeWrite, H, S, subcommand
from tests.test_ops import recording

SECRET = "ghp_" + "S" * 36  # synthetic


# --- Pre-flight -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("auto_commit", "auto_push", "stray", "blocked", "warns"),
    [
        (True, False, [], False, False),
        (True, False, ["stray file"], True, False),
        (False, True, ["stray file"], True, False),  # DEC-23: autoPush alone commits
        (True, True, ["stray file"], True, False),
        (False, False, ["stray file"], False, True),
        (False, False, [], False, False),
    ],
)
async def test_preflight(auto_commit, auto_push, stray, blocked, warns):
    fake = FakeWrite(auto_commit=auto_commit, auto_push=auto_push, stray=stray)
    pf = await fake.ops().preflight()
    assert pf == Preflight(S, H, S, auto_commit or auto_push, tuple(stray))
    assert (pf.blocked, pf.warns) == (blocked, warns)
    # dump-config first, then porcelain in its working tree; nothing writes.
    assert [c[:4] for c in fake.log] == [
        ("chezmoi", "--no-tty", "dump-config", "--format=json"),
        ("git", "-C", str(S), "status"),
    ]


async def test_preflight_uses_the_effective_working_tree():
    fake = FakeWrite(work_tree=Path("/repo"), source=Path("/repo/home"))
    pf = await fake.ops(source=Path("/elsewhere")).preflight()
    assert (pf.source_dir, pf.working_tree) == (Path("/repo/home"), Path("/repo"))
    assert fake.log[-1][:3] == ("git", "-C", "/repo")


async def test_failed_dump_config_is_masked_whole_and_never_clean():
    fake = FakeWrite()
    fake.fail("dump-config", f"template: line 1: token={SECRET}\nrendered {SECRET}\n".encode())
    with pytest.raises(ChezmoiFailed) as info:
        await fake.ops().preflight()
    e = info.value
    assert SECRET not in e.stderr + str(e) + repr(e)
    assert (e.returncode, e.last_line) == (1, MASK)
    assert e.__context__ is None
    assert e.__cause__ is None
    assert len(fake.log) == 1  # no git status, no write


async def test_failed_git_status_raises_masked():
    fake = FakeWrite(auto_commit=True)
    fake.fail("status", f"fatal: {SECRET}\n".encode(), 128)
    with pytest.raises(GitFailed) as info:
        await fake.ops().preflight()
    assert SECRET not in str(info.value) + info.value.stderr
    assert info.value.returncode == 128
    assert info.value.__context__ is None


async def test_unreadable_dump_config_is_chezmoi_unavailable():
    fake = FakeWrite()
    fake.reply(
        lambda argv, env: Completed(argv, 0, b"not json", b"") if "dump-config" in argv else None
    )
    with pytest.raises(ChezmoiUnavailable):
        await fake.ops().preflight()


def test_same_paths():
    pf = Preflight(S, H, S, True, ())
    assert pf.same_paths(Preflight(S, H, S, False, ("x",)))
    for other in (
        Preflight(H, H, S, True, ()),
        Preflight(S, S, S, True, ()),
        Preflight(S, H, H, True, ()),
    ):
        assert not pf.same_paths(other)


# --- Plan and result data -------------------------------------------------------


def test_command_display_quotes_and_argv_keeps_spaces():
    cz = Chezmoi(config=Path("/c d/chezmoi.toml"), source=Path("/s 'q'"))
    target = Path("/h/a file; rm -rf")
    argv = (*cz.argv("apply", "--force", "--recursive=false"), str(target))
    cmd = WriteCommand("apply", (target,), argv, False)
    assert shlex.split(cmd.display) == list(argv)
    assert "'/h/a file; rm -rf'" in cmd.display
    assert str(target) in cmd.argv
    assert "/c d/chezmoi.toml" in cmd.argv


def test_plan_states():
    clean = Preflight(S, H, S, True, ())
    dirty = Preflight(S, H, S, True, ("x",))
    apply = WriteCommand("apply", (H / "a",), ("chezmoi", "apply"), False)
    merge = WriteCommand("merge", (H / "a",), ("chezmoi", "merge"), True)
    assert not WritePlan(clean).blocked
    assert WritePlan(dirty).blocked
    assert WritePlan(clean, error="boom").blocked
    assert WritePlan(clean, commands=(apply, merge)).needs_confirmation
    assert not WritePlan(clean, commands=(merge,)).needs_confirmation


def test_result_ok():
    cmd = WriteCommand("apply", (H / "a",), ("chezmoi", "apply"), False)
    assert WriteResult((CommandResult(cmd, "ok"),)).ok
    assert WriteResult((CommandResult(cmd, "dry run"),)).ok
    assert not WriteResult((CommandResult(cmd, "failed"),)).ok
    assert not WriteResult(refused="blocked").ok


def test_diagnostics():
    assert short_error("", "apply", 2) == "chezmoi apply exited 2"
    assert short_error("first\n  last line  \n\n", "apply", 1) == "last line"
    text = f'token = "{SECRET}"\nplain\n'
    assert mask_diagnostic(None, text, whole=False) == f"{MASK}\n{MASK}\n"
    masker = Masker(Config(Path("/c")).secrets, H)
    assert mask_diagnostic(masker, text, whole=False) == f'token = "{MASK}"\nplain\n'
    assert mask_diagnostic(masker, text, whole=True) == f"{MASK}\n{MASK}\n"


# --- Live: the scratch setup ----------------------------------------------------


@requires_chezmoi
async def test_live_preflight_working_tree_differs_from_source(scratch: Scratch):
    source = scratch.source / "home"
    source.mkdir()
    scratch.config.write_text(f'workingTree = "{scratch.source}"\n[git]\n    autoCommit = true\n')
    (scratch.source / "README").write_text("outside the source dir\n")
    ops = WriteOps(
        Chezmoi(config=scratch.config, source=source, destination=scratch.home),
        Config(scratch.root / "cheztui.toml"),
    )
    pf = await ops.preflight()
    assert (pf.source_dir, pf.working_tree, pf.dest_dir) == (source, scratch.source, scratch.home)
    assert pf.stray == ("README",)
    assert pf.blocked
    scratch.commit_all("readme")
    assert not (await ops.preflight()).blocked


@requires_chezmoi
async def test_live_preflight_auto_commit_off_warns(scratch: Scratch):
    (scratch.source / "stray").write_text("x\n")
    pf = await WriteOps(scratch.chezmoi(), Config(scratch.root / "c.toml")).preflight()
    assert pf.warns
    assert not pf.blocked
    assert pf.stray == ("stray",)


# --- Preparation: unit ----------------------------------------------------------


def row(
    target: Path,
    direction: Direction = "src",
    fix: Fix = "apply",
    *,
    columns: str = " M",
    tags: tuple[str, ...] = (),
    kind: str | None = "file",
    source: Path | bool | None = True,
) -> DriftRow:
    if source is True:
        source = S / f"dot_{target.name}"
    return DriftRow(
        target=target,
        source=source or None,
        direction=direction,
        default_fix=fix,
        columns=columns,
        tags=tags,
        kind=kind,
        fix_overwrites=False,
    )


def previews(fake: FakeWrite) -> list[tuple[str, ...]]:
    return [c for c in fake.chezmoi_calls if "--dry-run" in c]


def diff_for(target: Path, line: str = "+x") -> bytes:
    return f"diff --git a/{target.name} b/{target.name}\n@@ -0,0 +1 @@\n{line}\n".encode()


def answer_previews(fake: FakeWrite, body: Callable[[Path], bytes] = diff_for) -> None:
    def reply(argv, env):
        words = subcommand(argv)
        if argv[0] == "chezmoi" and "--dry-run" in words:
            return Completed(argv, 0, body(Path(words[-1])), b"")
        return None

    fake.reply(reply)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for name in ("a", "b", "c", "t", "x"):
        (tmp_path / name).write_text("x\n")
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to("a")
    return tmp_path


@pytest.mark.parametrize(
    ("action", "kwargs", "reason"),
    [
        ("apply", {"source": None, "kind": None}, None),
        ("re-add", {}, None),
        ("re-add", {"source": None}, "no source"),
        ("re-add", {"tags": ("ext",)}, "an external"),
        ("re-add", {"tags": ("tmpl",), "name": "t"}, "a template"),
        ("re-add", {"kind": "script"}, "a script"),
        ("re-add", {"kind": "dir", "tags": ("dir",), "name": "d"}, "a directory"),
        ("re-add", {"kind": "symlink", "tags": ("link",), "name": "l"}, "a symlink"),
        ("re-add", {"columns": "DA"}, "target deleted"),
        ("re-add", {"name": "missing"}, "target deleted"),
        ("re-add", {"name": "d"}, "target isn't a regular file"),
        ("re-add", {"name": "l"}, "target isn't a regular file"),
        ("re-add", {"kind": None}, "no target-state entry"),
        ("merge", {"tags": ("tmpl",), "name": "t"}, None),
        ("merge", {"kind": "dir", "name": "d"}, "a directory"),
        ("merge", {"kind": "symlink", "name": "l"}, "a symlink"),
        ("merge", {"source": None}, "no source"),
        ("merge", {"kind": None}, "no target-state entry"),
        ("merge", {"columns": "DA"}, "target deleted"),
        ("forget", {"kind": "dir", "name": "d"}, None),
        ("forget", {"tags": ("ext",)}, "an external"),
        ("forget", {"source": None}, "no source"),
        ("edit", {"kind": "symlink", "name": "l"}, None),
        ("edit", {"columns": "DA"}, None),
        ("edit", {"kind": "dir", "name": "d"}, "not a file or symlink (dir)"),
        ("edit", {"kind": "script"}, "a script"),
        ("edit", {"source": None}, "no source"),
    ],
)
def test_eligibility(home: Path, action, kwargs, reason):
    name = kwargs.pop("name", "a")
    assert eligibility(action, row(home / name, **kwargs)) == reason


async def test_explicit_batch_previews_each_target_and_runs_one_command(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    a, b, t = home / "a", home / "b", home / "t"
    rows = [row(b, "you"), row(t, "you", tags=("tmpl",)), row(a, "you"), row(b, "you")]
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, b"{{ .x }}", b"") if "execute-template" in argv else None
        )
    )
    plan = await fake.ops().prepare("re-add", rows)
    assert [(e.target.name, e.selected, e.excluded) for e in plan.entries] == [
        ("a", True, None),
        ("b", True, None),
        ("t", False, "a template"),
    ]
    assert plan.commands == (
        WriteCommand(
            "re-add",
            (a, b),
            ("chezmoi", "--no-tty", "re-add", "--recursive=false", str(a), str(b)),
            False,
        ),
    )
    flags = ("--dry-run", "--verbose", "--no-pager", "--color=false", "--use-builtin-diff")
    assert previews(fake) == [
        ("re-add", "--recursive=false", *flags, str(a)),
        ("re-add", "--recursive=false", *flags, str(b)),
    ]
    assert plan.entries[0].diff == diff_for(a).decode()
    assert plan.entries[2].diff is None  # excluded: never previewed
    assert not plan.blocked
    assert plan.needs_confirmation


async def test_configured_flags_reach_every_command(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    flags: dict[str, Any] = {
        "config": Path("/c d/c.toml"),
        "source": S,
        "destination": home,
        "dry_run": True,
    }
    plan = await fake.ops(**flags).prepare("apply", [row(home / "a")])
    prefix = (
        "chezmoi", "--no-tty", "--config", "/c d/c.toml", "--source", str(S),
        "--destination", str(home), "--dry-run",
    )  # fmt: skip
    assert plan.prefix == prefix
    assert plan.dry_run
    assert plan.commands[0].argv[: len(prefix)] == prefix
    assert all(c[: len(prefix)] == prefix for c in fake.log if c[0] == "chezmoi")


async def test_empty_selection_has_no_command():
    fake = FakeWrite()
    for prepare in (
        fake.ops().prepare("apply", []),
        fake.ops().prepare_defaults([]),
        fake.ops().prepare_apply_all([]),
    ):
        plan = await prepare
        assert (plan.entries, plan.commands) == ((), ())
    assert fake.writes == []


async def test_all_excluded_has_no_command(home: Path):
    fake = FakeWrite(dest=home)
    plan = await fake.ops().prepare("re-add", [row(home / "a", source=None)])
    assert plan.commands == ()
    assert fake.writes == []


async def test_defaults_group_apply_readd_merge_in_drift_order(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    rows = [
        row(home / "x", "both", "merge", columns="MM"),
        row(home / "b", "you", "re-add", columns="MM"),
        row(home / "c", "src", "apply"),
        row(home / "a", "you", "re-add", columns="MM"),
        row(home / "t", "you", "merge", columns="MM", tags=("tmpl",), source=False),
        row(home / "d", "both", "apply", columns="MM", kind="dir", tags=("dir",)),
    ]
    plan = await fake.ops().prepare_defaults(rows)
    assert [(c.action, [t.name for t in c.targets], c.interactive) for c in plan.commands] == [
        ("apply", ["d", "c"], False),
        ("re-add", ["a", "b"], False),
        ("merge", ["x"], True),
    ]
    excluded = {e.target.name: e.excluded for e in plan.entries if e.excluded}
    assert excluded == {"t": "no source"}
    overwrites = {e.target.name for e in plan.entries if e.overwrites}
    assert overwrites == {"d"}
    # Merge runs suspended later: no dry-run merge (DEC-4, DEC-28).
    assert [p[0] for p in previews(fake)] == ["apply", "apply", "re-add", "re-add"]
    assert plan.needs_confirmation


async def test_interactive_only_plan_has_no_preview(home: Path):
    fake = FakeWrite(dest=home)
    plan = await fake.ops().prepare("merge", [row(home / "a", "both"), row(home / "b", "both")])
    assert [(c.action, len(c.targets)) for c in plan.commands] == [("merge", 2)]
    assert not plan.needs_confirmation
    assert previews(fake) == []
    with pytest.raises(ValueError, match="one target"):
        await fake.ops().prepare("edit", [row(home / "a"), row(home / "b")])


async def test_apply_all_ticks_and_reselection(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    a, b, c, x = (home / n for n in "abcx")
    rows = [
        row(a, "src", columns=" A"),  # never written: still ticked (DEC-16, #15)
        row(b, "you", "re-add", columns="MM"),
        row(c, "run", columns=" R", kind="script"),
        row(x, "both", "merge", columns="MM"),
    ]
    plan = await fake.ops().prepare_apply_all(rows)
    assert plan.apply_all
    assert [(e.target.name, e.selected, e.overwrites) for e in plan.entries] == [
        ("x", False, True),
        ("b", False, True),
        ("a", True, False),
        ("c", True, False),
    ]
    assert plan.commands[0].targets == (a, c)
    assert len(previews(fake)) == 4  # every row, ticked or not

    again = plan.with_selection([b, c])
    assert again.commands[0].targets == (b, c)  # Drift order
    assert [e.diff for e in again.entries] == [e.diff for e in plan.entries]
    assert plan.commands[0].targets == (a, c)  # the original is unchanged
    assert plan.with_selection([]).commands == ()
    assert len(previews(fake)) == 4


async def test_explicit_apply_warns_for_your_changes(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    plan = await fake.ops().prepare(
        "apply", [row(home / "a", "you", "re-add", columns="MM"), row(home / "b")]
    )
    assert [(e.target.name, e.overwrites) for e in plan.entries] == [("a", True), ("b", False)]


@pytest.mark.parametrize("action", ["apply", "re-add", "forget", "merge", "edit"])
async def test_blocked_preflight_runs_no_write_or_dry_run(home: Path, action):
    fake = FakeWrite(dest=home, auto_commit=True, stray=["stray"])
    plan = await fake.ops().prepare(action, [row(home / "a", "you", columns="MM")])
    assert plan.blocked
    assert plan.preflight.stray == ("stray",)
    assert all(e.diff is None for e in plan.entries)
    assert fake.writes == []
    assert len(plan.commands) == 1  # still shown, but blocked


async def test_auto_commit_off_warns_and_previews(home: Path):
    fake = FakeWrite(dest=home, stray=["stray"])
    answer_previews(fake)
    plan = await fake.ops().prepare("apply", [row(home / "a")])
    assert plan.preflight.warns
    assert not plan.blocked
    assert plan.entries[0].diff is not None


async def test_preview_failure_blocks_with_masked_error(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    fake.reply(
        lambda argv, env: (
            Completed(argv, 1, b"", f"x\nerror: token={SECRET}\n".encode())
            if argv[-1].endswith("/b") and "--dry-run" in argv
            else None
        )
    )
    plan = await fake.ops().prepare("apply", [row(home / n) for n in "abc"])
    assert plan.blocked
    assert plan.error == f"error: token={MASK}"
    assert SECRET not in plan.diagnostic + repr(plan)
    assert len(previews(fake)) == 2  # c is never previewed


async def test_previews_mask_like_masker(home: Path):
    fake = FakeWrite(dest=home)
    config = Config(Path("/c"))
    config = replace(config, secrets=replace(config.secrets, always_mask=("~/c",)))
    body = f'--- a\n+++ b\n@@ -1 +1 @@\n-password = "a\\"{SECRET}"\n+password = "b\\\\"\n'
    answer_previews(fake, lambda t: body.encode())
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, b'{{ onepassword "x" }}', b"")
            if "execute-template" in argv
            else None
        )
    )
    rows = [
        row(home / "a"),
        row(home / "b", tags=("enc",)),
        row(home / "c"),
        row(home / "t", tags=("tmpl",)),
    ]
    plan = await fake.ops(config).prepare("apply", rows)
    masker = Masker(config.secrets, home)
    entries = {e.target.name: e for e in plan.entries}
    assert entries["a"].diff == masker.mask(body, home / "a", diff=True).text
    assert (
        entries["a"].diff
        == f'--- a\n+++ b\n@@ -1 +1 @@\n-password = "{MASK}"\n+password = "{MASK}"\n'
    )
    for name, reason in (("b", "encrypted"), ("c", "always_mask"), ("t", "secret_template")):
        assert entries[name].masking == (reason,)
        assert entries[name].diff == masker.mask(body, home / name, diff=True, whole=True).text
    assert SECRET not in repr(plan)
    reads = [c for c in fake.chezmoi_calls if c[0] == "execute-template"]
    assert reads == [("execute-template", '{{ include "' + str(S) + '/dot_t" }}')]


def managed_json(entries: Mapping[str, str | None], home: Path) -> bytes:
    return json.dumps(
        {
            rel: {
                "absolute": str(home / rel),
                "sourceAbsolute": str(S / src) if src else None,
                "sourceRelative": src,
            }
            for rel, src in entries.items()
        }
    ).encode()


async def test_forget_directory_includes_descendants(home: Path):
    fake = FakeWrite(dest=home)
    answer_previews(fake)
    (home / "d" / "sub").mkdir()
    everything = {
        "d": "dot_d",
        "d/k": "dot_d/k",
        "d/sub": "dot_d/sub",
        "d/sub/s": "dot_d/sub/s",
        "a": "dot_a",
    }

    def reply(argv, env):
        words = subcommand(argv)
        if words[:1] == ("managed",):
            if "--include=encrypted" in words:
                return Completed(argv, 0, managed_json({"d/k": "dot_d/k"}, home), b"")
            if any(w.startswith("--include") for w in words):
                return Completed(argv, 0, b"{}", b"")
            return Completed(argv, 0, managed_json(everything, home), b"")
        return None

    fake.reply(reply)
    d = home / "d"
    rows = [
        row(d / "sub" / "s", "you", columns="MM"),
        row(d, kind="dir", tags=("dir",)),
        row(home / "a"),
    ]
    plan = await fake.ops().prepare("forget", rows)
    assert [(str(e.target.relative_to(home)), e.implied_by, e.direction) for e in plan.entries] == [
        ("a", None, "src"),
        ("d", None, "src"),
        ("d/k", d, None),
        ("d/sub", d, None),
        ("d/sub/s", d, "you"),
    ]
    assert plan.commands[0].targets == (home / "a", d)
    assert plan.commands[0].argv[-4:] == ("forget", "--force", str(home / "a"), str(d))
    # The directory and each descendant are previewed; the aggregate goes whole for d/k.
    assert [Path(p[-1]).name for p in previews(fake)] == ["a", "d", "k", "sub", "s"]
    entries = {e.target.name: e for e in plan.entries}
    assert entries["k"].masking == ("encrypted",)
    assert (
        entries["d"].diff
        == Masker(Config(Path("/c")).secrets, home)
        .mask(diff_for(d).decode(), d, diff=True, whole=True)
        .text
    )
    assert entries["s"].diff == diff_for(d / "sub" / "s").decode()
    # Descendants follow their directory's tick.
    off = plan.with_selection([home / "a", d / "sub" / "s"])
    assert [e.selected for e in off.entries] == [True, False, False, False, False]
    assert off.commands[0].targets == (home / "a",)


async def test_forget_directory_aggregate_ignores_never_mask(home: Path):
    fake = FakeWrite(dest=home)
    config = Config(Path("/c"))
    config = replace(config, secrets=replace(config.secrets, never_mask=("~/**",)))
    body = f"@@ -1 +0,0 @@\n-token = {SECRET}\n"
    answer_previews(fake, lambda t: body.encode())
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, managed_json({"d": "dot_d", "d/k": "dot_d/k"}, home), b"")
            if subcommand(argv)[:1] == ("managed",)
            and not any(w.startswith("--include") for w in argv)
            else (Completed(argv, 0, b"{}", b"") if subcommand(argv)[:1] == ("managed",) else None)
        )
    )
    plan = await fake.ops(config).prepare("forget", [row(home / "d", kind="dir", tags=("dir",))])
    entries = {e.target.name: e for e in plan.entries}
    assert entries["d"].diff == f"@@ -1 +0,0 @@\n-token = {MASK}\n"
    assert entries["k"].diff == body  # its own preview keeps never_mask


# --- Preparation: live ----------------------------------------------------------


def live_ops(scratch: Scratch, chezmoi: Chezmoi | None = None) -> WriteOps:
    return WriteOps(chezmoi or scratch.chezmoi(), Config(scratch.root / "cheztui.toml"))


def snapshot(scratch: Scratch) -> tuple[str, str, dict[str, bytes]]:
    """HEAD, porcelain and every file under the scratch home and source."""
    files = {
        str(p): p.read_bytes()
        if p.is_file() and not p.is_symlink()
        else oct(p.lstat().st_mode).encode()
        for root in (scratch.home, scratch.source)
        for p in sorted(root.rglob("*"))
        if ".git" not in p.relative_to(root).parts
    }
    head = scratch.git("rev-parse", "HEAD").stdout
    return head, scratch.git("status", "--porcelain").stdout, files


async def live_rows(scratch: Scratch) -> dict[str, DriftRow]:
    return {
        str(r.target.relative_to(scratch.home)): r for r in (await drift(scratch.chezmoi())).rows
    }


def setup_three(scratch: Scratch) -> None:
    """With autoCommit on: `.a` you→, `.b` src→, `.c` you→, all committed."""
    scratch.set_auto_commit(True)
    h = scratch.home
    for name in (".a", ".b", ".c"):
        (h / name).write_text(f"{name}\n")
    scratch.run("add", *(str(h / n) for n in (".a", ".b", ".c")))
    with (h / ".a").open("a") as f:
        f.write("home edit\n")
    with (h / ".c").open("a") as f:
        f.write("home edit\n")
    with (scratch.source / "dot_b").open("a") as f:
        f.write("source edit\n")
    scratch.commit_all("source edit")


@requires_chezmoi
async def test_live_previews_write_nothing(scratch: Scratch):
    setup_three(scratch)
    rows = await live_rows(scratch)
    before = snapshot(scratch)
    cz, rec = recording(scratch)
    ops = live_ops(scratch, cz)
    for action, name in (("re-add", ".a"), ("apply", ".b"), ("forget", ".c")):
        plan = await ops.prepare(action, [rows[name]])
        assert not plan.blocked, plan.error
        assert plan.entries[0].diff, action
        assert snapshot(scratch) == before, action
    assert "+home edit" in ((await ops.prepare("re-add", [rows[".a"]])).entries[0].diff or "")
    writes = [c for c in rec.calls if subcommand(c)[0] in ("re-add", "apply", "forget")]
    assert writes
    assert all("--dry-run" in c for c in writes)


@requires_chezmoi
@pytest.mark.parametrize("action", ["apply", "re-add", "forget", "merge", "edit"])
async def test_live_dirty_auto_commit_blocks(scratch: Scratch, action: Action):
    setup_three(scratch)
    rows = await live_rows(scratch)
    (scratch.source / "stray").write_text("x\n")
    before = snapshot(scratch)
    cz, rec = recording(scratch)
    plan = await live_ops(scratch, cz).prepare(action, [rows[".a"]])
    assert plan.blocked
    assert plan.preflight.stray == ("stray",)
    assert [subcommand(c)[0] for c in rec.calls] == ["dump-config"]
    assert snapshot(scratch) == before


@requires_chezmoi
async def test_live_auto_commit_off_warns_and_previews(scratch: Scratch):
    setup_three(scratch)
    scratch.set_auto_commit(False)
    rows = await live_rows(scratch)
    (scratch.source / "stray").write_text("x\n")
    plan = await live_ops(scratch).prepare("apply", [rows[".b"]])
    assert plan.preflight.warns
    assert "+source edit" in (plan.entries[0].diff or "")


@requires_chezmoi
async def test_live_directory_plans(scratch: Scratch):
    h = scratch.home
    (h / ".d" / "sub").mkdir(parents=True)
    (h / ".d" / "x").write_text("x\n")
    (h / ".d" / "sub" / "y").write_text("y\n")
    scratch.run("add", str(h / ".d"))
    scratch.commit_all("add .d")
    (h / ".d").chmod(0o700)  # the directory drifts
    with (h / ".d" / "x").open("a") as f:
        f.write("child edit\n")
    rows = await live_rows(scratch)
    assert rows[".d"].kind == "dir"
    ops = live_ops(scratch)

    forget = await ops.prepare("forget", [rows[".d"]])
    assert [str(e.target.relative_to(h)) for e in forget.entries] == [
        ".d", ".d/sub", ".d/sub/y", ".d/x",
    ]  # fmt: skip
    assert forget.commands[0].targets == (h / ".d",)
    assert all(e.diff is not None for e in forget.entries)
    assert "-y" in (forget.entries[2].diff or "")  # in sync, still named

    apply = await ops.prepare("apply", [rows[".d"]])
    assert [e.target for e in apply.entries] == [h / ".d"]
    assert apply.commands[0].argv[-4:] == ("apply", "--force", "--recursive=false", str(h / ".d"))


@requires_chezmoi
@pytest.mark.parametrize("name", ['dot_we "ird" \\ name.tmpl', "dot_café {{ x }}.tmpl"])
async def test_live_source_text_is_literal(scratch: Scratch, name: str):
    body = '{{ .chezmoi.os }} {{ include "nothing" }}\n`raw` "q"\n'
    (scratch.source / name).write_text(body)
    assert await scratch.chezmoi().source_text(scratch.source / name) == body


@requires_chezmoi
async def test_live_previews_are_masked(scratch: Scratch):
    h = scratch.home
    (h / ".cfg").write_text('password = "old\\"value"\nplain\n')
    (h / ".netrc").write_text("machine x login y\n")
    scratch.run("add", str(h / ".cfg"), str(h / ".netrc"))
    (scratch.source / "dot_cfg").write_text(f'password = "new\\\\"\nplain\ntoken: {SECRET}\n')
    (scratch.source / "dot_netrc").write_text("machine x login z\n")
    scratch.commit_all("edits")
    rows = await live_rows(scratch)
    plan = await live_ops(scratch).prepare("apply", [rows[".cfg"], rows[".netrc"]])
    diffs = {e.target.name: e.diff or "" for e in plan.entries}
    assert f'-password = "{MASK}"' in diffs[".cfg"]
    assert f'+password = "{MASK}"' in diffs[".cfg"]
    assert f"+token: {MASK}" in diffs[".cfg"]
    assert " plain" in diffs[".cfg"]
    assert "-" + MASK in diffs[".netrc"]
    assert "+" + MASK in diffs[".netrc"]
    assert "login" not in diffs[".netrc"]
    assert SECRET not in repr(plan)
    assert plan.entries[1].masking == ("always_mask",)
