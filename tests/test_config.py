"""cheztui's config file (spec §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cheztui.config import (
    ConfigError,
    RefreshConfig,
    SecretsConfig,
    UiConfig,
    config_path,
    load,
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "cheztui" / "config.toml"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_from_a_missing_file(path: Path):
    config = load(path)
    assert config.path == path
    assert config.ui == UiConfig("textual-dark", 3, 100)
    assert config.refresh == RefreshConfig(True, 300)
    assert config.secrets == SecretsConfig(
        ("token", "secret", "password", "passwd", "api[_-]?key"),
        True,
        ("~/.config/karabiner/**", "~/.config/ghostty/config"),
        ("~/.netrc", "~/.ssh/id_*", "**/.env"),
    )
    assert not path.exists()
    assert not path.parent.exists()


def test_load_defaults_to_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write(tmp_path / "cheztui" / "config.toml", "[ui]\ndiff_context = 7\n")
    config = load()
    assert config.path == tmp_path / "cheztui" / "config.toml"
    assert config.ui.diff_context == 7


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}, "/x/cheztui/config.toml"),
        ({"HOME": "/h"}, "/h/.config/cheztui/config.toml"),
        ({"XDG_CONFIG_HOME": "", "HOME": "/h"}, "/h/.config/cheztui/config.toml"),
    ],
)
def test_config_path(environ, expected):
    assert config_path(environ) == Path(expected)


def test_values_are_read(path: Path):
    write(
        path,
        """
[ui]
theme = "nord"
diff_context = 0
narrow_breakpoint = 1

[refresh]
watch = false
debounce_ms = 0

[secrets]
mask_patterns = ["x+"]
token_formats = false
never_mask = []
always_mask = ["~/.a"]
""",
    )
    config = load(path)
    assert config.ui == UiConfig("nord", 0, 1)
    assert config.refresh == RefreshConfig(False, 0)
    assert config.secrets == SecretsConfig(("x+",), False, (), ("~/.a",))


def test_a_partial_table_keeps_the_other_defaults(path: Path):
    write(path, "[ui]\ntheme = 'nord'\n")
    config = load(path)
    assert config.ui == UiConfig(theme="nord")
    assert config.refresh == RefreshConfig()


def test_unknown_keys_and_tables_are_kept_through_set_and_save(path: Path):
    write(
        path,
        """[ui]
future = "kept"

[adopt]
roots = ["~/.config"]
""",
    )
    config = load(path)
    config.set("ui.theme", "nord")
    config.set("refresh.debounce_ms", 50)
    config.save()
    text = path.read_text()
    assert 'future = "kept"' in text
    assert 'roots = ["~/.config"]' in text
    again = load(path)
    assert again.ui.theme == "nord"
    assert again.refresh.debounce_ms == 50


def test_comments_and_order_survive_a_save(path: Path):
    original = """# my settings
[refresh]
debounce_ms = 300   # fast enough

[ui]
# context lines
diff_context = 3
theme = "nord"
"""
    write(path, original)
    config = load(path)
    config.set("ui.diff_context", 5)
    config.save()
    assert path.read_text() == original.replace("diff_context = 3", "diff_context = 5")


def test_save_creates_the_file(path: Path):
    config = load(path)
    config.set("secrets.token_formats", False)
    config.save()
    assert load(path).secrets.token_formats is False
    assert "[secrets]" in path.read_text()


def test_save_through_a_symlink_keeps_it(tmp_path: Path, path: Path):
    real = write(tmp_path / "dotfiles" / "cheztui.toml", "[ui]\ntheme = 'nord'\n")
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    config = load(path)
    config.set("ui.narrow_breakpoint", 80)
    config.save()
    assert path.is_symlink()
    assert path.resolve() == real.resolve()
    assert load(real).ui.narrow_breakpoint == 80


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[ui\n", "not valid TOML"),
        ("ui = 3\n", "ui must be a table"),
        ("refresh = []\n", "refresh must be a table"),
        ("[ui]\ndiff_context = true\n", "ui.diff_context must be an integer ≥ 0"),
        ("[ui]\ndiff_context = 1.5\n", "ui.diff_context must be an integer ≥ 0"),
        ("[ui]\ndiff_context = -1\n", "ui.diff_context must be an integer ≥ 0"),
        ("[refresh]\nwatch = 1\n", "refresh.watch must be true or false"),
        ("[refresh]\ndebounce_ms = -1\n", "refresh.debounce_ms must be an integer ≥ 0"),
        ("[ui]\nnarrow_breakpoint = 0\n", "ui.narrow_breakpoint must be an integer ≥ 1"),
        ('[secrets]\nmask_patterns = ["("]\n', r"secrets.mask_patterns\[0\] is not a valid"),
        ("[secrets]\nmask_patterns = [1]\n", "secrets.mask_patterns must be a list of strings"),
        ('[secrets]\nnever_mask = "x"\n', "secrets.never_mask must be a list of strings"),
        ('[ui]\ntheme = ""\n', "ui.theme must be a non-empty string"),
        ("[ui]\ntheme = 3\n", "ui.theme must be a non-empty string"),
    ],
)
def test_errors(path: Path, text: str, message: str):
    write(path, text)
    with pytest.raises(ConfigError, match=message) as exc:
        load(path)
    assert exc.value.path == path
    assert str(exc.value).startswith(f"{path}: ")


def test_a_file_that_is_not_utf8_is_invalid_toml(path: Path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"[ui]\ntheme = '\xff'\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(path)


@pytest.mark.parametrize(
    "key", ["secrets.mask_patterns", "secrets.never_mask", "adopt.roots", "ui", "nope"]
)
def test_set_refuses_lists_and_unknown_keys(path: Path, key: str):
    config = load(path)
    with pytest.raises(ConfigError, match=key):
        config.set(key, ["x"])
    assert config.doc.unwrap() == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ui.diff_context", -1),
        ("ui.diff_context", True),
        ("ui.narrow_breakpoint", 0),
        ("refresh.watch", "yes"),
        ("ui.theme", ""),
    ],
)
def test_set_validates_like_load(path: Path, key: str, value: object):
    config = load(path)
    with pytest.raises(ConfigError, match=key):
        config.set(key, value)
    assert config.ui == UiConfig()
    assert config.refresh == RefreshConfig()
    assert config.doc.unwrap() == {}


def test_set_updates_the_typed_config(path: Path):
    config = load(path)
    config.set("refresh.watch", False)
    assert config.refresh == RefreshConfig(watch=False)
    assert config.doc.unwrap() == {"refresh": {"watch": False}}
