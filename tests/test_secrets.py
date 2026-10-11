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
