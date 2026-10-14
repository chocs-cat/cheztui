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

In `tui/`, `app.py` is the frame (header, view tabs, log line, key bar),
its reads and its one write worker, with the terminal runner
(`run_in_terminal`, the app's `terminal_runner`) that runs edit, merge and
`d` under `App.suspend()`; `keys.py` declares every key once, and the
Textual bindings, the key bar, help and the palette are all generated from
it; `views/base.py` is the list and detail panes with the filter and the
selection, which each view in `views/` fills (`drift.py` so far, with the
Drift actions' keys); `widgets/diff.py` colours a unified diff by line
kind, and `widgets/confirm.py` is the preview modal over a `WritePlan`;
`screens.py` holds help, the palette and the session log, and
`settings.py` the settings screen.

Every write goes through `ops.WriteOps` (spec §5 Write flow; DEC-8,
DEC-27 to DEC-30), one instance per front end:

- `preflight()` reads chezmoi's effective source, destination and working
  tree from `dump-config`, and the stray files from `git status` in that
  working tree. With auto-commit on (`git.autoCommit` or `git.autoPush`) a
  dirty tree blocks; with it off the stray files are a warning. A failed
  read raises, its output masked; it never counts as a clean tree.
- `prepare(action, rows)`, `prepare_defaults(rows)` and
  `prepare_apply_all(rows)` turn Drift rows into a `WritePlan`: eligible
  and excluded entries, each target's masked dry-run preview, and the exact
  argv of one command per action group. Previews run only after a
  pre-flight that doesn't block. `WritePlan.with_selection` re-ticks a plan
  without running anything.
- `execute(plan, confirmed=…)` re-runs the pre-flight before each command,
  runs it once, and reports the commit from `HEAD` before and after, the
  push state, and masked output, stopping at the first failure. A plan
  only describes a write; executing it always checks again.
- Scripted `edit` and `add` (§8) build one-entry plans and execute through
  the same path, with verification afterwards. Scripted edits use the
  internal `_editor`, a 0600 temporary spec and an expected-result hash
  (DEC-18, DEC-31).
- Each `WriteCommand.env` reaches only its command.
- Interactive writes (edit, merge) take a front end's runner
  (`interactive_runner`, plus `interactive_env`); `chezmoi.py` still builds
  the argv, and `ops` still owns the pre-flight and the result. In dry-run
  mode they never start.

Plans and results hold masked text only: raw chezmoi output stays inside
the write path. The scripted CLI commands and the Drift view's actions
drive `WriteOps`, and both word a command's result with `ops.outcome_line`
(DEC-35); the TUI only lays out, suspends and logs. Tests also drive
`WriteOps` directly, with `tests/fake_write.py` for fakes and the scratch
setup for the real binary.

TUI tests run the app with `App.run_test()` on `tests/fake_chezmoi.py`,
which extends the write path's fake to answer chezmoi's and git's reads
from a canned state; its `drifted()` state replays the recorded fixtures,
and `drifted(root)` puts them under a real root with the home's files
created, so eligibility sees what chezmoi left. Tests replace the app's
`terminal_runner`, since a headless run can't suspend. A conftest fixture
makes the app's watcher a fake from `tests/watching.py`, whose `due()`
stands in for a change; the tests that watch real files set
`app.watcher_factory` to `watch.Watcher` and use that file's helpers,
which poll rather than sleep and show a missing refresh with a sentinel
change after it.
Snapshot tests (pytest-textual-snapshot) keep the expected screens as SVGs
in `tests/__snapshots__/`. After a deliberate change to what a screen
shows, rewrite them with `uv run pytest --snapshot-update` and review the
changed SVGs in the pull request like code: open them in a browser, or
`rsvg-convert -w 1400 x.svg -o x.png`. A failed run writes
`snapshot_report.html` with the differences. Snapshots run the app with
`App.run()`, where Textual starts tasks eagerly: a worker on the fake can
finish before `run_worker` returns, which `run_test()` never shows.

Tests never touch your real chezmoi source or home directory: they build
scratch ones and pass them to chezmoi with `--source`, `--destination` and
`--config` (see AGENTS.md, "Live chezmoi: rules"). The tests that run
chezmoi need chezmoi 2.60 or later on `PATH` and skip without it; CI runs
them against 2.60.0, the oldest supported. The parsers' recorded output is in
`tests/fixtures/chezmoi/`, whose README says how to record it again.

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
each runtime dependency at the version the release's `uv.lock` pins. It also
needs Rust at build time, since watchfiles builds from its sdist (DEC-12). To
redo a version's formula, run the **Formula** workflow by hand with that
version.
Locally, point `--root` at a checkout of its tag, which the lock comes from:
`uv run --script scripts/formula.py X.Y.Z --root ../cheztui-vX.Y.Z > cheztui.rb`.
