# Contributing

## Setup

```sh
uv sync
make check                    # everything below, as CI runs it
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check               # types (ty is pinned: it's pre-1.0)
uv run cheztui --help         # the dev install
```

The design is [docs/spec.md](docs/spec.md). Its §10 lays out the modules,
each depending only on the ones above it; they arrive with the milestones:

| Module | Role |
|---|---|
| `runner.py` | the async subprocess runner `chezmoi.py` and `git.py` share, which tests can swap |
| `chezmoi.py` | async wrapper over the `chezmoi` binary, with typed parsers for its output |
| `git.py` | source-repo queries: porcelain status, ahead/behind, log, `HEAD` |
| `config.py` | `~/.config/cheztui/config.toml`, loaded and saved with tomlkit |
| `edit.py` | the ops-spec engine and the `_editor` entry point |
| `secrets.py`, `state.py`, `preview.py`, `watch.py` | masking, chezmoi's persistent state, new-machine previews, file watching |
| `ops.py` | the operations, returning dataclasses |
| `cli.py` | argparse front end, human and `--json` output |
| `tui/` | the Textual app, its views and widgets |

The CLI and the TUI both go through `ops`, so put behaviour changes there.
Tests never touch your real chezmoi source or home directory: they build
scratch ones and pass them to chezmoi with `--source`, `--destination` and
`--config` (see AGENTS.md, "Live chezmoi: rules").

## Roadmap and decisions

Planned work and ideas are [GitHub issues](https://github.com/chocs-cat/cheztui/issues),
grouped into [milestones](https://github.com/chocs-cat/cheztui/milestones)
v0.1 to v0.5. Look there before starting something, and open one for anything
you won't finish now. Settled design questions are in
[docs/decisions.md](docs/decisions.md). Coding agents also follow
[AGENTS.md](AGENTS.md).

Work on a branch and open a pull request; `master` only takes squash merges,
and only once the **CI passed** check is green. That check needs lint (ruff
and ty), the tests on Linux and macOS with Python 3.11 to 3.13, and a
packaging check that installs the built wheel and sdist and runs `cheztui`.
PR titles must be Conventional Commits. Dependabot opens grouped update PRs
weekly (`ci(deps)`, `build(deps)`), which don't trigger a release on their
own.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). The
version and changelog are generated from them:

- `feat: …` → minor bump
- `fix: …`, `perf: …` → patch bump
- `feat!: …` or a `BREAKING CHANGE:` footer → major bump (minor while < 1.0)
- `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `chore:` → no release on their own, and not listed in the changelog

Pull request titles are checked for this format in CI.

## Releasing

Releases are automated with [release-please](https://github.com/googleapis/release-please):

1. Merge conventional commits to `master`.
2. release-please opens or updates a **release PR** that bumps the version
   (in `pyproject.toml`, `uv.lock`, `src/cheztui/__init__.py` and
   `.claude-plugin/plugin.json`) and adds a section to `CHANGELOG.md`. The
   first `feat` releases 0.1.0.
3. Merging that PR tags `vX.Y.Z` and creates the GitHub release. The
   `publish` job then builds the package and uploads it to PyPI, and the
   `formula` workflow writes the Homebrew formula, pushes it to
   [chocs-cat/homebrew-tap](https://github.com/chocs-cat/homebrew-tap), and
   installs it from there on macOS to check it.

Don't edit versions or `CHANGELOG.md` by hand. To force a specific version,
add a `Release-As: 1.0.0` footer to a commit.

### One-time setup

- **PyPI trusted publishing:** on pypi.org, add a pending publisher for
  project `cheztui`: owner `chocs-cat`, repo `cheztui`, workflow
  `release.yml`, environment `pypi`.
- **GitHub environment:** create an environment named `pypi` in the repo
  settings. Optionally require approval there.
- **Actions permissions:** in Settings → Actions → General, allow GitHub
  Actions to create pull requests.
- **Homebrew tap:** the `TAP_TOKEN` organization secret, a fine-grained
  personal access token for `chocs-cat/homebrew-tap` only, with **Contents**
  read/write, shared with the other repos whose releases write to the tap.
  If the secret is limited to selected repositories, add this one.
- **CI on release PRs:** a PR opened with the built-in `GITHUB_TOKEN` waits
  for a maintainer to approve its CI run ("action required"). The release
  workflow uses the `RELEASE_PLEASE_TOKEN` secret instead when it exists: a
  fine-grained personal access token for this repo only, with **Contents**
  and **Pull requests** read/write. Release PRs are then opened under your
  name and CI runs on them by itself. Renew the token before it expires; if
  it lapses, release-please fails until you replace it or delete the secret.
- **Branch protection:** require **CI passed** and **conventional commit
  titles** on `master`. GitHub Free doesn't offer protection on private
  repos, so this waits until the repo is public.

### Homebrew

The formula is `Formula/cheztui.rb` in
[chocs-cat/homebrew-tap](https://github.com/chocs-cat/homebrew-tap), written by
`scripts/formula.py`: don't edit it in the tap, change the script's template.
It builds from the PyPI sdist, depends on `chezmoi`, and has a `resource` for
each runtime dependency at the version the release's `uv.lock` pins. To redo a
version's formula, run the **Formula** workflow by hand with that version.
Locally, point `--root` at a checkout of its tag, which the lock comes from:
`uv run --script scripts/formula.py X.Y.Z --root ../cheztui-vX.Y.Z > cheztui.rb`.
