"""Masking contracts from spec §5 Secrets; all token values are synthetic."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cheztui.config import SecretsConfig
from cheztui.secrets import MASK, Masked, Masker, calls_secret_function, glob_matches


@pytest.mark.parametrize(
    ("pattern", "relative", "expected"),
    [
        ("~/.config/karabiner/**", ".config/karabiner", True),
        ("~/.config/karabiner/**", ".config/karabiner/a/b.json", True),
        ("~/.config/karabiner/**", ".config/karabiner-other/a", False),
        ("~/.config/ghostty/config", ".config/ghostty/config", True),
        ("~/.config/ghostty/config", ".config/ghostty/other", False),
        ("~/.netrc", ".netrc", True),
        ("~/.netrc", ".netrc.bak", False),
        ("~/.ssh/id_*", ".ssh/id_ed25519", True),
        ("~/.ssh/id_*", ".ssh/id_ed25519/x", False),
        ("~/.ssh/id_*", ".ssh/known_hosts", False),
        ("**/.env", ".env", True),
        ("**/.env", "a/b/.env", True),
        ("**/.env", "a/b/.env.local", False),
        (".config/*/config", ".config/ghostty/config", True),
        (".config/*/config", ".config/a/b/config", False),
        ("*", ".hidden", True),
        ("Config", "config", False),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("[ab].txt", "a.txt", True),
        ("[!a].txt", "b.txt", True),
        ("[!a].txt", "a.txt", False),
        ("[a-c].txt", "b.txt", True),
        ("[].txt", "[].txt", True),
        ("[]a].txt", "].txt", True),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/**/b", "a/b", True),
        ("a/**", "a", True),
        ("a/**", "ab", False),
    ],
)
def test_globs(tmp_path: Path, pattern: str, relative: str, expected: bool):
    assert glob_matches(pattern, tmp_path / relative, tmp_path) is expected


def test_absolute_glob(tmp_path: Path):
    pattern = str(tmp_path / "other" / "*.txt")
    assert glob_matches(pattern, tmp_path / "other" / "a.txt", tmp_path / "destination")
    assert not glob_matches(pattern, tmp_path / "a.txt", tmp_path)


def test_destination_is_literal(tmp_path: Path):
    home = tmp_path / "destination[1]*"
    assert glob_matches("**/.env", home / ".env", home)
    assert not glob_matches("**/.env", tmp_path / "destination1" / ".env", home)


@pytest.mark.parametrize(
    "source",
    [
        '{{ onepasswordRead "op://x" }}',
        '{{ (bitwarden "item" "x").login.password }}',
        '{{ pass "a/b" }}',
        '{{ include "k.age" | decrypt }}',
        '{{ keyring "svc" .user }}',
        "{{ onepassword $item }}",
        "{{ passRaw (`item`) }}",
        "{{ secretJSON .item }}",
    ],
)
def test_secret_calls(source: str):
    assert calls_secret_function(source)


@pytest.mark.parametrize(
    "family",
    [
        "onepassword",
        "bitwarden",
        "lastpass",
        "keepassxc",
        "keeper",
        "dashlane",
        "doppler",
        "gopass",
        "rbw",
        "protonPass",
        "awsSecretsManager",
        "hcpVaultSecret",
        "ejsonDecrypt",
    ],
)
def test_secret_function_families(family: str):
    assert calls_secret_function("{{ " + family + 'Read "item" }}')
    assert calls_secret_function("{{ .item | " + family + "Read }}")


@pytest.mark.parametrize(
    "name",
    [
        "azureKeyVault",
        "pass",
        "passFields",
        "passRaw",
        "passhole",
        "secret",
        "secretJSON",
        "keyring",
        "vault",
        "decrypt",
    ],
)
def test_secret_function_names(name: str):
    assert calls_secret_function("{{ " + name + ' "item" }}')


@pytest.mark.parametrize(
    "source",
    [
        "pass\n",
        "# pass the tests",
        "password",
        "{{ .user }}",
        "ordinary text",
        '{{ xonepasswordRead "item" }}',
        '{{ onepasswordRead1 "item" }}',
        '{{ secretExtra "item" }}',
        '{{ PASS "item" }}',
    ],
)
def test_non_calls(source: str):
    assert not calls_secret_function(source)


@pytest.mark.parametrize("reason", ["always", "encrypted", "template"])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_whole_masking(tmp_path: Path, reason: str, ending: str):
    masker = Masker(SecretsConfig(), tmp_path)
    target = tmp_path / (".netrc" if reason == "always" else "ordinary")
    kwargs = {"encrypted": reason == "encrypted", "secret_template": reason == "template"}
    text = ending.join(["a", "", "b", "last"])
    assert masker.masks_whole(target, **kwargs)
    assert masker.mask(text, target, **kwargs) == Masked(ending.join([MASK, "", MASK, MASK]), True)


@pytest.mark.parametrize("text", ["ordinary\n\nbody", "", "\n\r\n"])
def test_unmasked_body(tmp_path: Path, text: str):
    masker = Masker(SecretsConfig(), tmp_path)
    assert not masker.masks_whole(tmp_path / "ordinary")
    assert masker.mask(text, tmp_path / "ordinary") == Masked(text, False)


def test_whole_reason_wins_over_never_mask(tmp_path: Path):
    config = SecretsConfig(never_mask=("**",), always_mask=(".netrc",))
    masker = Masker(config, tmp_path)
    for target, kwargs in [
        (tmp_path / ".netrc", {}),
        (tmp_path / "ordinary", {"encrypted": True}),
        (tmp_path / "ordinary", {"secret_template": True}),
    ]:
        assert masker.mask("value\n", target, line_level=False, **kwargs) == Masked(
            MASK + "\n", True
        )


def test_empty_whole_body_and_frozen_result(tmp_path: Path):
    assert Masker(SecretsConfig(), tmp_path).mask("\n\r\n", tmp_path / ".netrc") == Masked(
        "\n\r\n", False
    )
    result = Masked("text", False)
    with pytest.raises(FrozenInstanceError):
        setattr(result, "text", "other")  # noqa: B010 (test mutation of a frozen result)


TOKENS = [
    *("gh" + kind + "_" + "A" * 36 for kind in "pousr"),
    "github_pat_" + "A_" * 20,
    "sk-" + "a_B-" * 8,
    *(prefix + "A" * 16 for prefix in ("AKIA", "ASIA")),
    *("xox" + kind + "-" + "aB1-" * 5 for kind in "abposr"),
]


@pytest.mark.parametrize("token", TOKENS, ids=lambda t: t[:4])
@pytest.mark.parametrize("wrapper", ["export GH={}", "https://example.test/?credential={}&x=1"])
def test_token_formats(tmp_path: Path, token: str, wrapper: str):
    text = wrapper.format(token)
    result = Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config")
    assert result == Masked(wrapper.format(MASK), True)


@pytest.mark.parametrize("token", TOKENS, ids=lambda t: t[:4])
@pytest.mark.parametrize("prefix", ["x", "1", "_"])
def test_token_boundaries(tmp_path: Path, token: str, prefix: str):
    text = "value=" + prefix + token
    assert Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config") == Masked(text, False)


@pytest.mark.parametrize("suffix", ["A", "1"])
@pytest.mark.parametrize("prefix", ["AKIA", "ASIA"])
def test_aws_end_boundary(tmp_path: Path, prefix: str, suffix: str):
    text = "value=" + prefix + "A" * 16 + suffix
    assert Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config") == Masked(text, False)


@pytest.mark.parametrize("token", TOKENS, ids=lambda t: t[:4])
def test_token_formats_disabled(tmp_path: Path, token: str):
    masker = Masker(SecretsConfig(token_formats=False), tmp_path)
    assert masker.mask("value=" + token, tmp_path / "config") == Masked("value=" + token, False)
    assert masker.mask("token=" + token, tmp_path / "config") == Masked("token=" + MASK, True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('api_key = "v"', 'api_key = "********"'),
        ("  password: v", "  password: ********"),
        ("- token: v", "- token: ********"),
        ("export GITHUB_TOKEN=v", "export GITHUB_TOKEN=********"),
        ('"secret": "v",', '"secret": "********",'),
        ("Password=v", "Password=********"),
        ("passwd = 'v'  # comment", "passwd = '********'  # comment"),
        ("'api-key': 'v',", "'api-key': '********',"),
        ("section.api_key=v", "section.api_key=********"),
        ("token=v # trailing comment", "token=********"),
        ('secret = "unterminated', 'secret = "********'),
        ("token=value,  \t", "token=********,  \t"),
        ("token=value  ,  ", "token=********  ,  "),
        ("token=", "token="),
        ("token=  ", "token=  "),
        ('token=""', 'token=""'),
        ("secret: {", "secret: {"),
        ("secret: [  ", "secret: [  "),
        ("other=v", "other=v"),
        ("prefix token=v", "prefix token=v"),
        ("token v", "token v"),
        ("token+=v", "token+=v"),
    ],
)
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_name_patterns(tmp_path: Path, text: str, expected: str, ending: str):
    result = Masker(SecretsConfig(), tmp_path).mask(text + ending, tmp_path / "config")
    assert result == Masked(expected + ending, text != expected)


QUOTED_VALUE_CASES = [
    ("json", r'"password": "first\"still-secret",', '"password": "********",'),
    ("toml", r'password = "first\"still-secret" # keep', 'password = "********" # keep'),
    ("odd-three", r'password = "first\\\"still-secret"', 'password = "********"'),
    ("even-two", r'password = "first\\" # keep "later"', 'password = "********" # keep "later"'),
    ("even-four", r'password = "first\\\\" # keep', 'password = "********" # keep'),
    ("unterminated", r'password = "first\"still-secret', 'password = "********'),
    ("multiple-escaped", r'password = "\"first\"still-secret\"', 'password = "********'),
    ("trailing-backslash", 'password = "first\\', 'password = "********'),
    ("single-literal", "password = 'first\\' # keep", "password = '********' # keep"),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(text, expected) for _, text, expected in QUOTED_VALUE_CASES],
    ids=[name for name, _, _ in QUOTED_VALUE_CASES],
)
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_quoted_value_delimiters(tmp_path: Path, text: str, expected: str, ending: str):
    result = Masker(SecretsConfig(), tmp_path).mask(text + ending, tmp_path / "config")
    assert result == Masked(expected + ending, True)


def test_custom_name_patterns_replace_defaults(tmp_path: Path):
    masker = Masker(SecretsConfig(mask_patterns=("credential$",)), tmp_path)
    assert masker.mask("credential=v\npassword=v", tmp_path / "config") == Masked(
        "credential=" + MASK + "\npassword=v", True
    )


def test_merged_and_separate_spans(tmp_path: Path):
    token = "ghp_" + "A" * 36
    masker = Masker(SecretsConfig(), tmp_path)
    assert masker.mask('token="' + token + '"', tmp_path / "config") == Masked(
        'token="' + MASK + '"', True
    )
    # Separate spans preserve the closing quote between them.
    separate = 'secret="' + "a" * 20 + '"sk-' + "B" * 20
    assert masker.mask(separate, tmp_path / "config") == Masked(
        'secret="' + MASK + '"' + MASK, True
    )
    assert masker.mask(token + " / " + token, tmp_path / "config") == Masked(
        MASK + " / " + MASK, True
    )


@pytest.mark.parametrize("kind", ["RSA ", "OPENSSH ", ""])
@pytest.mark.parametrize("closed", [False, True])
def test_pem_blocks(tmp_path: Path, kind: str, closed: bool):
    begin = "-----BEGIN " + kind + "PRIVATE KEY-----"
    end = "-----END " + kind + "PRIVATE KEY-----"
    text = begin + "\nbody one\n\nbody two\n"
    expected = begin + "\n" + MASK + "\n" + MASK + "\n" + MASK + "\n"
    if closed:
        text += end + "\nafter\n"
        expected += end + "\nafter\n"
    assert Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config") == Masked(
        expected, True
    )
    assert Masker(SecretsConfig(token_formats=False), tmp_path).mask(
        text, tmp_path / "config"
    ) == Masked(text, False)


def test_public_pem_unchanged(tmp_path: Path):
    text = "-----BEGIN PUBLIC KEY-----\nbody\n-----END PUBLIC KEY-----"
    assert Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config") == Masked(text, False)


@pytest.mark.parametrize("line_level", [True, False])
def test_line_level_opt_out(tmp_path: Path, line_level: bool):
    text = 'token = "v"\n' + "ghp_" + "A" * 36 + "\n-----BEGIN PRIVATE KEY-----\nbody\n"
    target = tmp_path / (".config/karabiner/config" if line_level else "config")
    assert Masker(SecretsConfig(), tmp_path).mask(text, target, line_level=line_level) == Masked(
        text, False
    )


def test_forced_whole_and_never_mask_override(tmp_path: Path):
    # Write diagnostics classify grouped output themselves (spec §5 Diagnostics).
    masker = Masker(SecretsConfig(never_mask=("**",)), tmp_path)
    target = tmp_path / "keys"
    text = 'token = "v"\nplain\n'
    assert masker.mask(text, target) == Masked(text, False)
    assert masker.mask(text, target, never_mask=False) == Masked(f'token = "{MASK}"\nplain\n', True)
    assert masker.mask(text, target, whole=True) == Masked(f"{MASK}\n{MASK}\n", True)
    diff = "@@ -1 +1 @@\n-a\n+b\n"
    assert (
        masker.mask(diff, target, diff=True, whole=True).text == f"@@ -1 +1 @@\n-{MASK}\n+{MASK}\n"
    )


def test_template_call_masks_rendered_body(tmp_path: Path):
    source = '{{ onepassword "item" }}'
    assert Masker(SecretsConfig(), tmp_path).mask(
        "rendered value\n", tmp_path / "config", secret_template=calls_secret_function(source)
    ) == Masked(MASK + "\n", True)


# Each case declares the content-line indexes independently of the masker.
# The invariant test below checks every case, in each masking mode.
DIFF_CASES = [
    *(
        (
            "quoted-" + name,
            "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-" + text + "\n+" + text + "\n",
            "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-" + expected + "\n+" + expected + "\n",
            {3, 4},
        )
        for name, text, expected in QUOTED_VALUE_CASES
    ),
    (
        "toml",
        "diff --git a/config b/config\nindex 000..111 100644\n--- a/config\n+++ b/config\n"
        '@@ -1,2 +1,2 @@\n-token = "old"\n+token = "new"\n other = "keep"\n',
        "diff --git a/config b/config\nindex 000..111 100644\n--- a/config\n+++ b/config\n"
        '@@ -1,2 +1,2 @@\n-token = "********"\n+token = "********"\n other = "keep"\n',
        {5, 6, 7},
    ),
    (
        "pem",
        "@@ -1,3 +1,3 @@\n -----BEGIN RSA PRIVATE KEY-----\n-old\n+new\n"
        " -----END RSA PRIVATE KEY-----\n",
        "@@ -1,3 +1,3 @@\n -----BEGIN RSA PRIVATE KEY-----\n-********\n+********\n"
        " -----END RSA PRIVATE KEY-----\n",
        {1, 2, 3, 4},
    ),
    (
        "pem-across-hunks",
        "@@ -1,2 +1,2 @@\n -----BEGIN PRIVATE KEY-----\n-old\n+new\n"
        "@@ -20,3 +20,3 @@\n more\n -----END PRIVATE KEY-----\n after\n",
        "@@ -1,2 +1,2 @@\n -----BEGIN PRIVATE KEY-----\n-********\n+********\n"
        "@@ -20,3 +20,3 @@\n ********\n -----END PRIVATE KEY-----\n after\n",
        {1, 2, 3, 5, 6, 7},
    ),
    (
        "header-like-content",
        "@@ -1,2 +1 @@\n--- comment\n-- token: v\n+other\n--- a/token=v\n+++ b/token=v\n"
        "@@ -1 +1 @@\n-secret=v\n+secret=w\n",
        "@@ -1,2 +1 @@\n--- comment\n-- token: ********\n+other\n"
        "--- a/token=v\n+++ b/token=v\n@@ -1 +1 @@\n-secret=********\n+secret=********\n",
        {1, 2, 3, 7, 8},
    ),
    (
        "new-file",
        "@@ -0,0 +1,3 @@\n+password: v\n+other\n+secret=w\n--- a/token=v\n",
        "@@ -0,0 +1,3 @@\n+password: ********\n+other\n+secret=********\n--- a/token=v\n",
        {1, 2, 3},
    ),
    (
        "deleted-file",
        "@@ -1,2 +0,0 @@\n-token=v\n-other\n+++ b/token=v\n",
        "@@ -1,2 +0,0 @@\n-token=********\n-other\n+++ b/token=v\n",
        {1, 2},
    ),
    (
        "omitted-counts-and-no-newline",
        "@@ -1 +1 @@\n-token=v\n\\ No newline at end of file\n+token=w\n"
        "\\ No newline at end of file\n--- a/token=v",
        "@@ -1 +1 @@\n-token=********\n\\ No newline at end of file\n+token=********\n"
        "\\ No newline at end of file\n--- a/token=v",
        {1, 3},
    ),
    (
        "empty-context",
        "@@ -1,3 +1,3 @@\n\n \n-token=v\n+token=w\n--- a/token=v\n",
        "@@ -1,3 +1,3 @@\n\n \n-token=********\n+token=********\n--- a/token=v\n",
        {1, 2, 3, 4},
    ),
    (
        "pem-reset-at-next-file",
        "diff --git a/key b/key\n@@ -1,2 +1,2 @@\n -----BEGIN PRIVATE KEY-----\n-old\n+new\n"
        "diff --git a/config b/config\nold mode 100644\nnew mode 100755\n"
        "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-ordinary\n+unchanged\n",
        "diff --git a/key b/key\n@@ -1,2 +1,2 @@\n"
        " -----BEGIN PRIVATE KEY-----\n-********\n+********\n"
        "diff --git a/config b/config\nold mode 100644\nnew mode 100755\n"
        "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-ordinary\n+unchanged\n",
        {2, 3, 4, 11, 12},
    ),
    (
        "pem-empty-context",
        "@@ -1,4 +1,4 @@\n -----BEGIN PRIVATE KEY-----\n\n \n-body\n+new\n",
        "@@ -1,4 +1,4 @@\n -----BEGIN PRIVATE KEY-----\n\n \n-********\n+********\n",
        {1, 2, 3, 4, 5},
    ),
    (
        "unchanged",
        "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-old=value\n+new=value\n",
        "--- a/config\n+++ b/config\n@@ -1 +1 @@\n-old=value\n+new=value\n",
        {3, 4},
    ),
]


@pytest.mark.parametrize(
    ("name", "text", "expected", "hunk_lines"), DIFF_CASES, ids=[case[0] for case in DIFF_CASES]
)
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("reason", ["line", "always", "encrypted", "template", "never", "off"])
def test_diffs(
    tmp_path: Path,
    name: str,
    text: str,
    expected: str,
    hunk_lines: set[int],
    ending: str,
    reason: str,
):
    text = text.replace("\n", ending)
    expected = expected.replace("\n", ending)
    config = SecretsConfig(never_mask=("never",))
    target = tmp_path / ({"always": ".netrc", "never": "never"}.get(reason, "config"))
    result = Masker(config, tmp_path).mask(
        text,
        target,
        diff=True,
        encrypted=reason == "encrypted",
        secret_template=reason == "template",
        line_level=reason != "off",
    )
    before = text.splitlines(keepends=True)
    after = result.text.splitlines(keepends=True)
    assert len(before) == len(after)
    for index, (original, masked) in enumerate(zip(before, after, strict=True)):
        assert original[:1] == masked[:1]
        if index not in hunk_lines:
            assert original == masked
    if reason in ("always", "encrypted", "template"):
        for index in hunk_lines:
            content = before[index].removesuffix(ending)
            expected_line = content[:1] + (MASK if content[1:] else "")
            if before[index].endswith(ending):
                expected_line += ending
            assert after[index] == expected_line
        assert result.masked
    elif reason in ("never", "off"):
        assert result == Masked(text, False)
    else:
        assert result == Masked(expected, text != expected)


@pytest.mark.parametrize("token", TOKENS, ids=lambda t: t[:4])
def test_tokens_in_diff(tmp_path: Path, token: str):
    text = "@@ -1 +1 @@\n-value=" + token + "\n+value=ordinary\n"
    result = Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / "config", diff=True)
    assert result == Masked("@@ -1 +1 @@\n-value=" + MASK + "\n+value=ordinary\n", True)


def test_diff_lines_end_at_newline_alone(tmp_path: Path):
    # #55: a lone `\r` doesn't end a diff line (DEC-40), so the line after it is
    # still in the hunk and masked.
    text = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-old\n+secret=abc\rleaked\n password=hunter2\n"
    masker = Masker(SecretsConfig(), tmp_path)
    whole = masker.mask(text, tmp_path / "x", diff=True, whole=True)
    assert whole == Masked(f"--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-{MASK}\n+{MASK}\n {MASK}\n", True)
    line_level = masker.mask(text, tmp_path / "x", diff=True).text
    assert line_level.endswith(f" password={MASK}\n")
    assert "leaked" not in line_level


def test_headers_and_zero_count_hunk_unchanged(tmp_path: Path):
    token = "ghp_" + "A" * 36
    text = "diff --git a/" + token + " b/config\n@@ -0,0 +0,0 @@\n-token=v\n"
    assert Masker(SecretsConfig(), tmp_path).mask(text, tmp_path / ".netrc", diff=True) == Masked(
        text, False
    )
