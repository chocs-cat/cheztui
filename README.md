<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/chocs-cat/cheztui/master/docs/logo-dark.svg">
  <img alt="cheztui" src="https://raw.githubusercontent.com/chocs-cat/cheztui/master/docs/logo-light.svg" height="64">
</picture>

A clean, fast TUI for [chezmoi](https://www.chezmoi.io/): see what drifted,
which side changed, and fix it with one key.

cheztui opens on the files where your home directory and your chezmoi source
disagree, says which side changed, and offers the right fix: re-add, apply or
merge. Every write shows a preview first, and the result names the commit
chezmoi made. A `--json` CLI covers the same ground for scripts and coding
agents, including `cheztui edit` for safe scripted edits to managed files.

**Status:** v0.1 is the Drift view and the agent CLI (`status`, `verify`,
`edit`, `add`). The other views in [docs/spec.md](docs/spec.md) arrive with
the later [milestones](https://github.com/chocs-cat/cheztui/milestones).

## Install

cheztui needs [chezmoi](https://www.chezmoi.io/) 2.60 or later. Install it one
of two ways; either gives you the `cheztui` command.

### Homebrew

```sh
brew install chocs-cat/tap/cheztui
```

The formula lives in the `chocs-cat/tap` tap and brings its own Python and
chezmoi.

### PyPI

```sh
uv tool install cheztui
```

This installs cheztui from PyPI into its own environment. It needs Python
3.11 or later and chezmoi 2.60 or later on your `PATH`; `pipx install cheztui`
works the same way.

## Usage

```sh
cheztui                   # the TUI, on the Drift view (? for its keys)
cheztui status            # what drifted, which side changed, and the fix
cheztui status --json     # the same rows as JSON, for scripts and agents
cheztui verify            # ✓ verify, or ✗ verify and the failing targets (exit 1)
cheztui verify --json
cheztui edit ~/.zshrc --ops ops.json --dry-run  # preview source-text changes
cheztui edit ~/.zshrc --ops ops.json --json     # apply, report the commit, verify
cheztui add ~/.new-config --dry-run            # preview a new file or directory
cheztui add ~/.new-config --json               # add, report the commit, verify
```

```
both  MM  ~/.claude/settings.json  merge
you→  MM  ~/.zshrc                 re-add
src→   M  ~/.gitconfig             apply   tmpl
run    R  ~/install-brew.sh        apply
```

`you→` means you edited the target, `src→` that the source changed, `both`
that both did, and `run` that a script will run on the next apply. Global
flags go before the command and are passed to every chezmoi call:
`-n/--dry-run`, `--chezmoi-config PATH`, `--source PATH`,
`--destination PATH`. Exit codes: `0` ok, `1` refused, failed or not verified, `2` usage or
config error, `3` chezmoi missing, too old (cheztui needs 2.60 or later) or
failing.

### The TUI

`cheztui` opens on the Drift view: each drifted row's direction, status and
tags, and a detail pane with the default fix and chezmoi's diff, masked (`t`
shows a template's rendered output, `R` reveals). Its keys run the fixes:
`enter` the row's default fix, `a` apply, `r` re-add, `m` merge, `e` edit the
source, `d` chezmoi's own diff, and, in help (`?`) and the command palette,
`M` merge the selection, `x` forget and `A` apply all. A fix shows a preview
(the exact command and each target's dry run) and runs on `enter`; edit and
merge go straight to your editor or merge tool. The log line then names the
commit chezmoi made and whether it pushed.

The view refreshes on its own when the source dir or a managed file changes,
`refresh.debounce_ms` (300 by default) after the last change; cheztui's own
writes refresh once. `g` refreshes by hand, and `refresh.watch = false` in
`~/.config/cheztui/config.toml`, or on the settings screen (`,`), turns
watching off. Filter, selection, the session log and settings are in help.

### Scripted edits

`edit` takes one managed regular file and a JSON ops spec (a file, or `--ops -`
for stdin). It refuses drift unless `--allow-drift` is given, and refuses a
configured chezmoi `edit.command`. Templates are edited as source text;
encrypted and `always_mask` diffs are hidden unless `--reveal` is passed.
With chezmoi auto-commit on, a dirty source tree blocks the edit; with it
off, stray files produce a warning. See [spec §8](docs/spec.md#8-cheztui-edit-and-the-agent-skill)
for the ops reference, refusals and JSON contract.

`add` takes one unmanaged file or directory, with `--template` and `--encrypt`
when needed. It previews the addition and refuses managed paths or directories
containing managed entries, so adoption cannot overwrite an existing source.

## Agent skill

`skills/cheztui/SKILL.md` teaches coding agents to check what drifted and edit
chezmoi-managed files through cheztui's `--json` CLI. The repo is also a
Claude Code plugin marketplace:

```
/plugin marketplace add chocs-cat/cheztui
/plugin install cheztui@cheztui
```

Or copy `skills/cheztui` into your agent's skills directory.

## Development

```sh
uv sync
make check            # ruff, ty and pytest, as CI runs them
uv run cheztui        # the dev install
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout and the release process.
Outside pull requests aren't taken yet; [issues](https://github.com/chocs-cat/cheztui/issues)
and bug reports are welcome.

## License

MIT; see [LICENSE](LICENSE).
