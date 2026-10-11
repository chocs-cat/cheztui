# Working on cheztui

Instructions for coding agents (Codex reads this file; Claude Code reads it
through `CLAUDE.md`'s `@AGENTS.md` import). Humans: see CONTRIBUTING.md.

cheztui is a Textual TUI and an argparse CLI for
[chezmoi](https://www.chezmoi.io/). It shows what drifted, which side
changed, and the fix, and runs the fix with one key after a preview. Its
`cheztui edit` command makes scripted, assertion-checked edits to managed
files for agents. `docs/spec.md` is the design; README.md covers use,
CONTRIBUTING.md the layout and the release process, and
`skills/cheztui/SKILL.md` how agents *use* cheztui.

The repo is a scaffold: the placeholder app and `--version` exist, and the
features in `docs/spec.md` §12 land milestone by milestone.

## Before you start

- Read `docs/decisions.md`. Those questions are settled: don't reopen one
  unless the user asks. When the user settles a new one, add an entry.
- Check the roadmap: `gh issue list`, and `gh api repos/chocs-cat/cheztui/milestones`
  for v0.1 to v0.5. Ideas, bugs and follow-ups are tracked there; the work in
  progress is in the relay's local files (see "Relay workflow").

## Commands

- `uv sync`: set up a fresh checkout.
- `make check`: ruff check, ruff format check, ty, pytest. Run it before every
  push. CI runs the same, plus a packaging check.
- `uv run pytest tests/test_cli.py -q`: one file, for the loop while working.

## Roadmap: GitHub Issues

The roadmap is the issue list on `chocs-cat/cheztui`, grouped into the
milestones `v0.1` to `v0.5` from `docs/spec.md` §12. Ideas, bugs and
follow-ups are issues. The design and delivery plan for anything bigger than
one chunk live in a relay project file, and each chunk in its brief; both
link the issues they come from. Keep the issues current:

- **File ideas as issues, don't just mention them.** When you or the user
  come up with a feature, a follow-up, a bug or a loose end you won't handle
  now, create an issue without asking first. Unplanned thoughts get the
  `idea` label. Add an area label and a type label (`bug`, `enhancement`,
  `question`) where one fits, and a milestone when the spec places the work.
  Area labels: `tui`, `cli`, `config`, `chezmoi`, `release`, `documentation`,
  and one per view or feature: `drift`, `files`, `scripts`, `data`,
  `externals`, `adopt`, `secrets`, `health`, `repo`, `preview`, `edit`,
  `skill`.
- **The repo will go public, and its issues with it.** Write them for any
  reader: no private paths, hostnames, dotfile contents or personal details.
- Before starting work, look for an existing issue and reference it. Close
  issues from the PR that resolves them (`Fixes #12` in the PR body).
- Comment on an issue when you learn something that matters to whoever
  picks it up next.

## Workflow

- **One branch and pull request per chunk.** Code changes arrive through
  relay chunks, each on a `chunk/<id>` branch with its own pull request;
  planning changes (docs, decisions, AGENTS.md) go on `plan/<slug>`
  branches. Don't commit to `master`; release-please is the only thing that
  does.
- Conventional Commits for PR titles (CI checks them): `feat:`, `fix:`,
  `perf:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `chore:`, with a
  scope where useful (`fix(tui): …`). The type decides the version bump and
  the changelog entry, so choose it for the user-visible effect. PRs are
  squash-merged, so the title is the commit release-please reads. Unit
  commits on a chunk branch keep their relay titles (`P0001-C03-a: …`) and
  never reach `master` on their own.
- Don't edit versions or `CHANGELOG.md`; release-please owns them. Merging
  a release PR publishes to PyPI and the Homebrew tap, so that is the
  maintainer's call. To force a version, add a `Release-As: X.Y.Z` footer.
- `master` only merges once **CI passed** is green; don't bypass it (no
  `gh pr merge --admin`).
- Keep README.md, CONTRIBUTING.md, `skills/cheztui/SKILL.md`, the TUI's help
  text and `docs/spec.md` in step with any behaviour change, in the same PR.
- Put behaviour in `ops.py`; the CLI and the TUI both call it.
- Every chezmoi call goes through `chezmoi.py`, and every write through the
  one write path (DEC-8, DEC-13).

## Live chezmoi: rules

cheztui writes to a real dotfiles repo, and chezmoi may auto-commit and
auto-push every change it makes. A test that reaches the user's own setup
can rewrite their home directory and publish the result. So:

- **Tests never touch the real source dir or home.** Each test builds a
  scratch HOME, a scratch source dir (a real git repo) and a scratch chezmoi
  config under `tmp_path`, and passes them to chezmoi with `--source`,
  `--destination` and `--config`. Set `HOME` (and `XDG_CONFIG_HOME`) to the
  scratch dirs too, so nothing falls back to the real defaults. The
  `scratch` fixture in `tests/conftest.py` does all of this: use it (its
  `chezmoi()` and `run()` pass the three flags, and `requires_chezmoi`
  skips without the binary) rather than building a setup by hand.
- Never run a writing chezmoi command (`apply`, `add`, `re-add`, `edit`,
  `forget`, `destroy`, `chattr`, `state delete`, `update`, `git push`)
  without all three flags pointing at scratch paths.
- The scratch config never enables `autoPush`, and the scratch source repo
  has no remote.
- Integration tests run `ops.py` against the real `chezmoi` binary and skip
  when it isn't installed.

## Testing notes

- Unit: parsers against recorded chezmoi output fixtures; the ops-spec
  engine (every op, count assertions, skip, failure leaves the file
  untouched); masking.
- TUI: `App.run_test()` with `tests/fake_chezmoi.py` (from v0.1) for canned
  states, and snapshot tests per view and for the preview modal.
- To look at the TUI: in `run_test`, `app.save_screenshot("x.svg")`, then
  `rsvg-convert -w 1400 x.svg -o x.png`.
- The shell may be zsh, which doesn't word-split `$var`; wrap bash-isms in
  `bash -c`.

## Relay workflow

Work in this repository is built by fresh agent sessions that hand it to one
another through local process files and pull requests, using the relay
plugin's skills (`/relay:<skill>` in Claude Code, `$relay:<skill>` in Codex):

- `relay:go` runs whichever step comes next, as the status file's *Next*
  block records it. Start every fresh session with it.
- `relay:project` shapes an idea into settled design and an ordered delivery
  plan, makes design changes, and closes the project when its work is
  accepted.
- `relay:plan` cuts the next chunk brief from the active project, or from a
  small, already-designed fix.
- `relay:execute` builds the chunk in flight unit by unit on its own branch
  and draft pull request, then hands it back for review.
- `relay:review` reviews the chunk in flight, records the review, and either
  sends it back for rework or merges it.
- `relay:release` cuts a version by reviewing and merging release-please's
  release pull request. It is only ever run on purpose, never by `relay:go`.

Code reaches the integration branch only through a chunk pull request, so every
change gets the executor's test discipline and a CI run. The status file is the
handoff between sessions: run each step in a fresh session.

The process files (status, chunk briefs, projects, and the two checklists)
live in `.relay/`, which git ignores: they are never committed, and there is
one copy whatever branch is checked out. A session in a linked worktree uses
the main working tree's `.relay/`. Never run `git clean -x` or `-X`: it
deletes them, and nothing else holds a copy. A chunk's remote record is its
pull request, whose body carries the executor's report and whose comments
carry the review. Tracked files (code, docs, the decision log, the changelog,
pull request titles) cite authority sections, decision IDs, issues, and pull
requests, never a chunk ID, a review finding, or a checklist entry.

When code and an authority doc disagree, one of them is wrong: fix it and say
which. A decided entry in the decision log is not re-litigated; where the docs
genuinely do not cover something, add an entry, update the doc that should have
said it, and cite the entry at the code site.

### Settings

- **Integration branch:** `master`. Chunk and planning pull requests
  squash-merge here.
- **Release branch:** same as the integration branch.
- **Gate:** `make check`, which runs ruff check, ruff format check, ty and
  pytest. It passes before every unit commit and push. CI runs these same
  targets, on Linux and macOS with Python 3.11 to 3.13, plus a packaging
  check that stays CI-only.
- **CI:** `.github/workflows/ci.yml` runs the gate on pull requests, including
  drafts; pushes to `master` run it too. The required check is **CI passed**;
  **conventional commit titles** checks the pull request title.
- **Releases:** release-please, as CONTRIBUTING.md's *Releasing* describes: a
  chunk pull request's title is a Conventional Commit for the chunk's
  user-visible effect, and nobody edits the changelog by hand. Unit commits on
  a chunk branch keep their relay titles; only the squash commit reaches
  `master`, titled without the chunk ID.
- **Issues:** GitHub Issues, as *Roadmap* describes: review follow-ups and
  ideas are filed as issues, a project or maintenance chunk may start from
  one, and the pull request that finishes it closes it.
- **Authority:** `docs/spec.md`, then `README.md` (use), `CONTRIBUTING.md`
  (layout and releases) and `skills/cheztui/SKILL.md` (agent use).
- **Status:** `.relay/status.md` (local)
- **Chunks:** `.relay/chunks/<ID>.md` (local)
- **Decision log:** `docs/decisions.md`, entries cited as `(DEC-n)`
- **Review checklist:** `.relay/review-checklist.md` (local)
- **Brief checklist:** `.relay/brief-checklist.md` (local)
- **Projects:** `.relay/projects/` (local; `index.md`, `template.md`, one
  `P####-slug.md` per project)
- **Tools:** `tools/`, review aids run by hand, outside the gate and the build
- **Review extras:** every review also checks, against the whole diff:
  - *Live-chezmoi isolation.* Every new or changed test that runs chezmoi
    passes scratch `--source`, `--destination` and `--config`, sets `HOME`
    and `XDG_CONFIG_HOME` to scratch dirs, never enables `autoPush`, and
    gives the scratch source repo no remote ("Live chezmoi: rules").
  - *Layering.* Behaviour lives in `ops.py`, not the CLI or the TUI; every
    chezmoi call goes through `chezmoi.py`, and every write through the one
    write path (DEC-8, DEC-13).
  - *Docs in step.* A behaviour change updates README.md, CONTRIBUTING.md,
    `skills/cheztui/SKILL.md`, the TUI's help text and `docs/spec.md` in the
    same pull request.
  - *Public-safe text.* The pull request body, review comments and any issue
    filed carry no private paths, hostnames, dotfile contents or personal
    details.
- **Environment:** none beyond `uv sync`.
