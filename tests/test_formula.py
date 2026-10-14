"""scripts/formula.py, which writes the Homebrew formula after a release."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement

from cheztui import __version__

ROOT = Path(__file__).parent.parent
PYPROJECT_0_1_0 = '[project]\nname = "cheztui"\nversion = "0.1.0"\n'


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("formula", ROOT / "scripts" / "formula.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["formula"] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


formula = _load()


def test_render():
    cheztui = formula.Sdist("cheztui", "https://files/cheztui-0.1.0.tar.gz", "a" * 64)
    text = formula.render(
        cheztui,
        [
            formula.Sdist("textual", "https://files/textual-8.2.8.tar.gz", "c" * 64),
            formula.Sdist("markdown-it-py", "https://files/markdown_it_py-4.2.0.tar.gz", "b" * 64),
        ],
    )
    assert text.startswith("class Cheztui < Formula\n  include Language::Python::Virtualenv\n")
    assert '  url "https://files/cheztui-0.1.0.tar.gz"\n  sha256 "' + "a" * 64 in text
    assert (
        '  depends_on "rust" => :build\n  depends_on "chezmoi"\n  depends_on "python@3.14"\n'
        in text
    )
    assert text.index('resource "markdown-it-py"') < text.index('resource "textual"')
    assert '    url "https://files/textual-8.2.8.tar.gz"\n    sha256 "' + "c" * 64 in text
    assert 'shell_output("#{bin}/cheztui --version")' in text
    assert 'shell_output(bin/"cheztui")' in text
    assert text.endswith("  end\nend\n")


def test_platform_markers():
    def needed(line: str) -> bool:
        return formula.needed(Requirement(line), "3.14")

    assert needed("rich==15.0.0")
    assert needed("appnope==0.1.4 ; sys_platform == 'darwin'")
    assert not needed("colorama==0.4.6 ; sys_platform == 'win32'")
    assert not needed("tomli==2.2.1 ; python_full_version < '3.11'")


def test_pins_are_the_locked_runtime_dependencies():
    pins = {r.name: str(r.specifier) for r in formula.pins()}
    assert pins["textual"].startswith("==")
    assert "pytest" not in pins  # dev dependencies stay out
    assert "cheztui" not in pins


def test_checkout_version(tmp_path):
    assert formula.checkout_version() == __version__
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_0_1_0)
    assert formula.checkout_version(tmp_path) == "0.1.0"


def test_refuses_another_versions_checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_0_1_0)
    with pytest.raises(SystemExit, match=r"is 0\.1\.0, not 0\.2\.0"):
        formula.main(["0.2.0", "--root", str(tmp_path)])
