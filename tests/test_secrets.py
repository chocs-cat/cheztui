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


def test_template_call_masks_rendered_body(tmp_path: Path):
    source = '{{ onepassword "item" }}'
    assert Masker(SecretsConfig(), tmp_path).mask(
        "rendered value\n", tmp_path / "config", secret_template=calls_secret_function(source)
    ) == Masked(MASK + "\n", True)
