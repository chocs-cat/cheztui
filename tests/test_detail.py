"""The Drift detail's reads in `ops.py` (spec §6 Rows and detail in the TUI; DEC-34).

`cut_context` is pure text; `masking_reasons` and `drift_body` run on the
fakes, and the live tests on the real chezmoi in the scratch setup.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cheztui.chezmoi import ChezmoiFailed
from cheztui.config import Config, SecretsConfig
from cheztui.ops import DriftRow, cut_context, drift, drift_body, masking_reasons
from cheztui.runner import Completed
from cheztui.secrets import MASK, Masker
from tests.conftest import Scratch, requires_chezmoi
from tests.fake_chezmoi import FakeChezmoi
from tests.fake_write import FakeWrite, S
from tests.test_chezmoi_reads import HOME
from tests.test_chezmoi_reads import build_drift as build_drift_setup
from tests.test_write import answer_previews, row

TOKEN = "ghp_" + "T" * 36  # synthetic


def lines(*rows: str) -> str:
    return "".join(f"{r}\n" for r in rows)


# --- cut_context (spec §6 Drift, `ui.diff_context`) -------------------------------

HEAD = lines("diff --git a/f b/f", "index 1111111..2222222 100644", "--- a/f", "+++ b/f")

# One change in the middle of seven lines, with chezmoi's three lines of context.
ONE = HEAD + lines("@@ -1,7 +1,7 @@", " a", " b", " c", "-d", "+D", " e", " f", " g")


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (0, HEAD + lines("@@ -4 +4 @@", "-d", "+D")),
        (1, HEAD + lines("@@ -3,3 +3,3 @@", " c", "-d", "+D", " e")),
        (2, HEAD + lines("@@ -2,5 +2,5 @@", " b", " c", "-d", "+D", " e", " f")),
        (3, ONE),
        (4, ONE),
        (10, ONE),
    ],
)
def test_one_hunk(context, expected):
    assert cut_context(ONE, context) == expected


def middle(run: int) -> str:
    """Two changes with `run` context lines between them, and one line either side."""
    between = [f" m{i}" for i in range(run)]
    n = run + 4
    return HEAD + lines(f"@@ -1,{n} +1,{n} @@", " a", "-b", "+B", *between, "-c", "+C", " z")


@pytest.mark.parametrize("context", [1, 2])
def test_a_middle_run_of_2n_stays_one_hunk(context):
    run = 2 * context
    n = run + 4
    expected = HEAD + lines(
        f"@@ -1,{n} +1,{n} @@", " a", "-b", "+B", *(f" m{i}" for i in range(run)), "-c", "+C", " z"
    )
    assert cut_context(middle(run), context) == expected


def test_a_middle_run_of_2n_plus_1_splits():
    # N = 1: lines 1 a, 2 b, 3-5 m0-m2, 6 c, 7 z.
    assert cut_context(middle(3), 1) == HEAD + lines(
        "@@ -1,3 +1,3 @@", " a", "-b", "+B", " m0", "@@ -5,3 +5,3 @@", " m2", "-c", "+C", " z"
    )
    # N = 2: lines 1 a, 2 b, 3-7 m0-m4, 8 c, 9 z.
    assert cut_context(middle(5), 2) == HEAD + lines(
        "@@ -1,4 +1,4 @@",
        " a",
        "-b",
        "+B",
        " m0",
        " m1",
        "@@ -6,4 +6,4 @@",
        " m3",
        " m4",
        "-c",
        "+C",
        " z",
    )


def test_n_of_0_splits_at_any_context_between_changes():
    assert cut_context(middle(1), 0) == HEAD + lines(
        "@@ -2 +2 @@", "-b", "+B", "@@ -4 +4 @@", "-c", "+C"
    )
    # Adjacent changes are one hunk.
    adjacent = HEAD + lines("@@ -1,3 +1,3 @@", " a", "-b", "+B", "-c", "+C")
    assert cut_context(adjacent, 0) == HEAD + lines("@@ -2,2 +2,2 @@", "-b", "+B", "-c", "+C")


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        # An addition after the last old line: the old count is 0, its start the line before.
        (
            lines("@@ -1,3 +1,4 @@", " a", " b", " c", "+d"),
            lines("@@ -3,0 +4 @@", "+d"),
        ),
        # A deletion: the new count is 0.
        (
            lines("@@ -1,4 +1,3 @@", " a", " b", " c", "-d"),
            lines("@@ -4 +3,0 @@", "-d"),
        ),
        # A new file and a deleted one keep their headers.
        (lines("@@ -0,0 +1,2 @@", "+a", "+b"), lines("@@ -0,0 +1,2 @@", "+a", "+b")),
        (lines("@@ -1 +0,0 @@", "-a"), lines("@@ -1 +0,0 @@", "-a")),
    ],
)
def test_zero_count_starts(diff, expected):
    assert cut_context(diff, 0) == expected


NO_NEWLINE = "\\ No newline at end of file"


def test_no_newline_marker_stays_after_its_line():
    diff = lines("@@ -1,4 +1,4 @@", " a", " b", " c", "-d", NO_NEWLINE, "+D", NO_NEWLINE)
    assert cut_context(diff, 1) == lines(
        "@@ -3,2 +3,2 @@", " c", "-d", NO_NEWLINE, "+D", NO_NEWLINE
    )


def test_no_newline_marker_on_a_split():
    # Each marker stays with its line in the second piece.
    diff = lines(
        "@@ -1,5 +1,5 @@", "-a", "+A", " b", " c", " d", "-e", NO_NEWLINE, "+E", NO_NEWLINE
    )
    assert cut_context(diff, 1) == lines(
        "@@ -1,2 +1,2 @@",
        "-a",
        "+A",
        " b",
        "@@ -4,2 +4,2 @@",
        " d",
        "-e",
        NO_NEWLINE,
        "+E",
        NO_NEWLINE,
    )
    # Context after a change, then the marker on context that is cut away.
    diff = lines("@@ -1,4 +1,4 @@", "-a", "+A", " b", " c", " d", NO_NEWLINE)
    assert cut_context(diff, 1) == lines("@@ -1,2 +1,2 @@", "-a", "+A", " b")


def test_two_files():
    second = lines(
        "diff --git a/g b/g", "--- a/g", "+++ b/g", "@@ -1,3 +1,3 @@", " x", "-y", "+Y", " z"
    )
    assert cut_context(ONE + second, 0) == (
        HEAD
        + lines("@@ -4 +4 @@", "-d", "+D")
        + lines("diff --git a/g b/g", "--- a/g", "+++ b/g", "@@ -2 +2 @@", "-y", "+Y")
    )


def test_lines_outside_hunks_pass_through():
    diff = lines(
        "diff --git a/f b/f",
        "old mode 100644",
        "new mode 100755",
        "@@ -1,3 +1,3 @@",
        " a",
        "-b",
        "+B",
        " c",
        "trailing text that isn't a hunk line",
    )
    assert cut_context(diff, 0) == lines(
        "diff --git a/f b/f",
        "old mode 100644",
        "new mode 100755",
        "@@ -2 +2 @@",
        "-b",
        "+B",
        "trailing text that isn't a hunk line",
    )


def test_empty_lines_are_context_and_endings_are_kept():
    diff = "@@ -1,4 +1,4 @@\r\n a\r\n\r\n-b\r\n+B\r\n \r\n"
    assert cut_context(diff, 1) == "@@ -2,3 +2,3 @@\r\n\r\n-b\r\n+B\r\n \r\n"
    assert cut_context("@@ -1 +1 @@\n-a\n+b", 0) == "@@ -1 +1 @@\n-a\n+b"


@pytest.mark.parametrize("context", [0, 1, 2, 3])
def test_an_empty_diff_is_unchanged(context):
    assert cut_context("", context) == ""


# --- One classification (spec §5 Drift command plans; DEC-34) ------------------------

SECRET_SOURCE = b'{{ onepassword "x" }}'


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for name in ("a", "b", "c", "t"):
        (tmp_path / name).write_text("x\n")
    return tmp_path


def classified(home: Path) -> tuple[FakeWrite, Config, list[DriftRow]]:
    """A plain file, an `enc` row, an `always_mask` target and a password-manager template."""
    fake = FakeWrite(dest=home)
    config = Config(Path("/c"))
    config = replace(config, secrets=replace(config.secrets, always_mask=("~/c",)))
    body = lines("--- a", "+++ b", "@@ -1 +1 @@", f"-token = {TOKEN}", f"+x {TOKEN}")
    answer_previews(fake, lambda t: body.encode())
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, body.encode(), b"")
            if "diff" in argv
            else Completed(argv, 0, SECRET_SOURCE, b"")
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
    return fake, config, rows


async def test_the_preview_and_the_detail_share_one_classification(home: Path):
    fake, config, rows = classified(home)
    plan = await fake.ops(config).prepare("apply", rows)
    masker = Masker(config.secrets, home)
    chezmoi = fake.chezmoi()
    expected = {"a": (), "b": ("encrypted",), "c": ("always_mask",), "t": ("secret_template",)}
    for r, entry in zip(rows, plan.entries, strict=True):
        reasons = await masking_reasons(chezmoi, masker, r.target, r.source, r.tags)
        assert entry.masking == reasons == expected[r.target.name]
        detail = await drift_body(chezmoi, config.secrets, r, home, context=3)
        assert detail.text == entry.diff
        assert detail.masked
        assert TOKEN not in detail.text
    whole = await drift_body(chezmoi, config.secrets, rows[1], home, context=3)
    assert whole.text == lines("--- a", "+++ b", "@@ -1 +1 @@", f"-{MASK}", f"+{MASK}")
    plain = await drift_body(chezmoi, config.secrets, rows[0], home, context=3)
    assert plain.text == lines("--- a", "+++ b", "@@ -1 +1 @@", f"-token = {MASK}", f"+x {MASK}")


async def test_a_template_without_a_secret_call_is_line_masked(home: Path):
    fake = FakeWrite(dest=home)
    fake.reply(
        lambda argv, env: (
            Completed(argv, 0, b"{{ .name }}", b"") if "execute-template" in argv else None
        )
    )
    reasons = await masking_reasons(
        fake.chezmoi(), Masker(SecretsConfig(), home), home / "t", S / "dot_t.tmpl", ("tmpl",)
    )
    assert reasons == ()
    # No source, or not a template: the source isn't read.
    calls = len(fake.log)
    masker = Masker(SecretsConfig(), home)
    assert await masking_reasons(fake.chezmoi(), masker, home / "t", None, ("tmpl",)) == ()
    assert await masking_reasons(fake.chezmoi(), masker, home / "t", S / "dot_t", ()) == ()
    assert len(fake.log) == calls


# --- drift_body on the fake (spec §6 Rows and detail in the TUI) ---------------------


async def fixture_rows(fake: FakeChezmoi) -> dict[str, DriftRow]:
    return {r.target.name: r for r in (await drift(fake.chezmoi())).rows}


def with_token(fake: FakeChezmoi) -> str:
    """`.tool`'s recorded diff, its added line holding a token."""
    diff = fake.diff[HOME / ".tool"].replace("+echo more", f"+echo {TOKEN}")
    fake.diff[HOME / ".tool"] = diff
    return diff


async def test_a_token_in_the_diff_is_masked():
    fake = FakeChezmoi.drifted()
    diff = with_token(fake)
    rows = await fixture_rows(fake)
    body = await drift_body(fake.chezmoi(), SecretsConfig(), rows[".tool"], HOME, context=3)
    assert body.masked
    assert body.text == diff.replace(TOKEN, MASK)
    assert ("diff", "--no-pager", "--color=false", "--use-builtin-diff", str(HOME / ".tool")) in (
        fake.chezmoi_calls
    )


async def test_never_mask_keeps_it():
    fake = FakeChezmoi.drifted()
    diff = with_token(fake)
    rows = await fixture_rows(fake)
    secrets = replace(SecretsConfig(), never_mask=("~/.tool",))
    body = await drift_body(fake.chezmoi(), secrets, rows[".tool"], HOME, context=3)
    assert (body.text, body.masked) == (diff, False)


async def test_reveal_keeps_it_and_masks_nothing():
    fake = FakeChezmoi.drifted()
    diff = with_token(fake)
    rows = await fixture_rows(fake)
    body = await drift_body(
        fake.chezmoi(), SecretsConfig(), rows[".tool"], HOME, context=3, reveal=True
    )
    assert (body.text, body.masked) == (diff, False)


async def test_nothing_to_mask():
    fake = FakeChezmoi.drifted()
    rows = await fixture_rows(fake)
    body = await drift_body(fake.chezmoi(), SecretsConfig(), rows[".mine"], HOME, context=3)
    assert (body.text, body.masked) == (fake.diff[HOME / ".mine"], False)


async def test_context_cuts_after_masking():
    fake = FakeChezmoi.drifted()
    diff = with_token(fake)
    rows = await fixture_rows(fake)
    body = await drift_body(fake.chezmoi(), SecretsConfig(), rows[".tool"], HOME, context=1)
    assert body.text == cut_context(diff.replace(TOKEN, MASK), 1)
    assert body.text.endswith("@@ -2 +2,2 @@\n echo tool\n+echo ********\n")
    assert body.masked


async def test_rendered_is_the_body_masked_as_a_body_and_not_cut():
    fake = FakeChezmoi.drifted()
    fake.cat[HOME / ".tmplfile"] = f"TMPL\nMORE\ntoken: {TOKEN}\n".encode()
    rows = await fixture_rows(fake)
    body = await drift_body(
        fake.chezmoi(), SecretsConfig(), rows[".tmplfile"], HOME, context=0, rendered=True
    )
    assert (body.text, body.masked) == (f"TMPL\nMORE\ntoken: {MASK}\n", True)
    assert ("cat", str(HOME / ".tmplfile")) in fake.chezmoi_calls
    revealed = await drift_body(
        fake.chezmoi(),
        SecretsConfig(),
        rows[".tmplfile"],
        HOME,
        context=0,
        rendered=True,
        reveal=True,
    )
    assert revealed.text == f"TMPL\nMORE\ntoken: {TOKEN}\n"


async def test_rendered_bytes_that_are_not_utf8_are_replaced():
    fake = FakeChezmoi.drifted()
    fake.cat[HOME / ".tmplfile"] = b"a\xffb\n"
    rows = await fixture_rows(fake)
    body = await drift_body(
        fake.chezmoi(), SecretsConfig(), rows[".tmplfile"], HOME, context=3, rendered=True
    )
    assert body.text == "a�b\n"


async def test_a_secret_template_is_masked_whole():
    fake = FakeChezmoi.drifted()
    fake.sources = dict.fromkeys(fake.sources, '{{ onepassword "x" }}\n')
    rows = await fixture_rows(fake)
    chezmoi, row_ = fake.chezmoi(), rows[".tmplfile"]
    diff = await drift_body(chezmoi, SecretsConfig(), row_, HOME, context=3)
    assert diff.text.endswith(f"@@ -1 +1,2 @@\n {MASK}\n+{MASK}\n")
    rendered = await drift_body(chezmoi, SecretsConfig(), row_, HOME, context=3, rendered=True)
    assert (rendered.text, rendered.masked) == (f"{MASK}\n{MASK}\n", True)


async def test_a_failing_diff_raises():
    fake = FakeChezmoi.drifted()
    rows = await fixture_rows(fake)
    fake.fail("diff", f"chezmoi: {TOKEN}\n".encode(), binary="chezmoi")
    with pytest.raises(ChezmoiFailed) as e:
        await drift_body(fake.chezmoi(), SecretsConfig(), rows[".mine"], HOME, context=3)
    assert e.value.returncode == 1  # raw, as `drift()`'s errors are: the front end masks


# --- Live: the installed chezmoi --------------------------------------------------


@requires_chezmoi
async def test_live_diff_of_a_you_row(scratch: Scratch):
    build_drift_setup(scratch)
    chezmoi = scratch.chezmoi()
    rows = {r.target.name: r for r in (await drift(chezmoi)).rows}
    body = await drift_body(chezmoi, SecretsConfig(), rows[".mine"], scratch.home, context=3)
    # chezmoi's diff goes from the target to the target state: applying would
    # drop the line the user added.
    assert "-edited in home\n" in body.text
    assert body.text.startswith("diff --git a/.mine b/.mine\n")
    assert not body.masked


@requires_chezmoi
async def test_live_rendered_template(scratch: Scratch):
    build_drift_setup(scratch)
    chezmoi = scratch.chezmoi()
    rows = {r.target.name: r for r in (await drift(chezmoi)).rows}
    row_ = rows[".tmplfile"]
    assert row_.tags == ("tmpl",)
    body = await drift_body(chezmoi, SecretsConfig(), row_, scratch.home, context=3, rendered=True)
    assert body.text == "TMPL\nMORE\n"
    diff = await drift_body(chezmoi, SecretsConfig(), row_, scratch.home, context=0)
    assert diff.text.endswith("@@ -1,0 +2 @@\n+MORE\n")
