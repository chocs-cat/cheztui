# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging>=24"]
# ///
"""Write cheztui's Homebrew formula for a version published on PyPI.

    git worktree add --detach ../cheztui-v0.1.0 v0.1.0
    uv run --script scripts/formula.py 0.1.0 --root ../cheztui-v0.1.0 > cheztui.rb

The formula builds from the PyPI sdist, with a `resource` for each runtime
dependency at the version `uv.lock` pins, so Homebrew installs what CI tested.
`--root` is a checkout of the release's tag (default: the checkout this script
is in): the lock comes from there, and the script refuses when its
`pyproject.toml` has another version. The template is this script's own, so a
fix to it applies when an older version's formula is redone. Each pin's sdist URL
and hash come from PyPI. A dependency whose markers hold on neither macOS nor
Linux is left out. Unlike `brew update-python-resources`, this doesn't skip
uploads under a day old, so it works right after a release. The release
workflow runs it after publishing; `--wait` retries while PyPI doesn't have the
version yet.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PACKAGE = "cheztui"
PYTHON = "3.14"  # Homebrew's newest; the formula depends on python@PYTHON
PYPI = "https://pypi.org/pypi"
ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = """\
class Cheztui < Formula
  include Language::Python::Virtualenv

  desc "Clean, fast TUI for chezmoi"
  homepage "https://github.com/chocs-cat/cheztui"
  url "{url}"
  sha256 "{sha256}"
  license "MIT"

  depends_on "rust" => :build
  depends_on "chezmoi"
  depends_on "python@{python}"
{resources}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/cheztui --version")
    assert_match "chezmoi", shell_output(bin/"cheztui")
  end
end
"""

RESOURCE = """
  resource "{name}" do
    url "{url}"
    sha256 "{sha256}"
  end
"""


@dataclass(frozen=True)
class Sdist:
    name: str
    url: str
    sha256: str


def render(package: Sdist, resources: list[Sdist], python: str = PYTHON) -> str:
    """The formula's text; resources in the alphabetical order brew audit wants."""
    blocks = "".join(
        RESOURCE.format(name=r.name, url=r.url, sha256=r.sha256)
        for r in sorted(resources, key=lambda r: r.name)
    )
    return TEMPLATE.format(url=package.url, sha256=package.sha256, python=python, resources=blocks)


def sdist(name: str, version: str, *, wait: float = 0) -> Sdist:
    """A release's sdist on PyPI, retrying for up to `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(f"{PYPI}/{name}/{version}/json", timeout=30) as r:
                release = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or time.monotonic() >= deadline:
                raise SystemExit(f"formula.py: {name} {version} isn't on PyPI ({e.code})") from e
            time.sleep(10)
    for f in release["urls"]:
        if f["packagetype"] == "sdist":
            return Sdist(canonicalize_name(name), f["url"], f["digests"]["sha256"])
    raise SystemExit(f"formula.py: {name} {version} has no sdist on PyPI")


def checkout_version(root: Path = ROOT) -> str:
    """The version in the working tree's pyproject.toml."""
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def pins(root: Path = ROOT) -> list[Requirement]:
    """The runtime dependencies `uv.lock` pins, for every platform."""
    r = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
         "--no-header", "--no-annotate"],
        cwd=root, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if r.returncode != 0:
        raise SystemExit(f"formula.py: can't read the pins from uv.lock:\n{r.stderr}")
    return [Requirement(line) for line in r.stdout.splitlines() if line.strip()]


def needed(req: Requirement, python: str) -> bool:
    """Whether a pin applies on macOS or Linux under the formula's Python."""
    if req.marker is None:
        return True
    return any(
        Marker(str(req.marker)).evaluate(
            {
                "sys_platform": platform,
                "platform_system": system,
                "python_version": python,
                "python_full_version": f"{python}.0",
            }
        )
        for platform, system in (("darwin", "Darwin"), ("linux", "Linux"))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write cheztui's Homebrew formula.")
    parser.add_argument("version", help="a version of cheztui published on PyPI")
    parser.add_argument("--python", default=PYTHON, help=f"the formula's Python (default {PYTHON})")
    parser.add_argument("--wait", type=float, default=0, help="seconds to wait for PyPI")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="a checkout of the release's tag (default: this)"
    )
    args = parser.parse_args(argv)

    if (here := checkout_version(args.root)) != args.version:
        raise SystemExit(f"formula.py: {args.root} is {here}, not {args.version}")
    package = sdist(PACKAGE, args.version, wait=args.wait)
    resources = []
    for req in pins(args.root):
        if not needed(req, args.python):
            continue
        (spec,) = req.specifier
        resources.append(sdist(req.name, spec.version))
    sys.stdout.write(render(package, resources, args.python))
    return 0


if __name__ == "__main__":
    sys.exit(main())
