---
name: cheztui
description: Change dotfiles that chezmoi manages with the `cheztui` CLI — "add an alias", "add a shell function", "change my gitconfig", "tweak my zshrc", "update the config under ~/.config/…" — through previewed, assertion-checked edits that chezmoi applies, commits and verifies in one command. Use this whenever a file to be changed is or might be chezmoi-managed, when a new file should be added to chezmoi, and when the user asks what drifted between their home directory and the chezmoi source.
---

# cheztui

cheztui is a TUI and a CLI for [chezmoi](https://www.chezmoi.io/). chezmoi
keeps a *source* copy of each managed file (its *target*, such as `~/.zshrc`)
in a git repo and applies it to the home directory. Editing the target
directly is lost on the next apply; editing the source by hand skips the
commit and the check. `cheztui edit` and `cheztui add` do the whole round
trip.

**Use the CLI with `--json`, not the TUI.** Bare `cheztui` opens an
interactive TUI meant for the human at the keys. Every command takes `--json`:
the result is JSON on stdout, notes and warnings go to stderr.

## Look before acting

```sh
cheztui status --json     # what drifted, which side changed, the fix
cheztui verify --json     # does every target match its source?
```

`status` gives `{"rows": [{target, source, direction, default_fix, columns,
tags}]}`, empty rows when in sync. `direction` is `you` (the target changed
in the home directory), `src` (the source changed and isn't applied yet),
`both`, or `run` (a script will run). `default_fix` is `re-add`, `apply` or
`merge`; `source` is `null` when the source no longer has the entry; `tags`
come from `tmpl`, `enc`, `exec`, `link`, `ext`, `dir`. `verify` gives
`{"verified": bool, "failing": [target, …]}` and exits 1 when not verified.

`cheztui edit` itself says whether a target is managed, and
`chezmoi source-path ~/.zshrc` prints the source file to read. For a setup
chezmoi can't find by default, the global flags go before the command and
reach every chezmoi call:

```sh
cheztui --source ~/dotfiles --destination ~ --chezmoi-config ~/.config/chezmoi/chezmoi.toml status --json
```

## Edit a managed file

1. **Read the source** (`chezmoi source-path <target>`, then that file) and
   copy each anchor out of it verbatim, leading whitespace and final newline
   included. An anchor typed from memory is the usual failure.
2. **Write the ops spec** to a temporary file outside the chezmoi source
   directory (a file there makes the source tree dirty).
3. **Dry run** and read the diff; fix the spec until it is exactly the
   change. Then **apply**: the same command without `--dry-run` runs
   `chezmoi edit --apply`, so chezmoi commits (and pushes, if set up to), and
   cheztui verifies the result.
4. **Report** the `commit` (sha and subject) and `pushed` to the user. After
   an edit to a shell startup file, tell them to `source` it or open a new
   shell.

```sh
cheztui edit ~/.zshrc --ops /tmp/zshrc-ops.json --dry-run --json
cheztui edit ~/.zshrc --ops /tmp/zshrc-ops.json --json
cheztui edit ~/.gitconfig --ops - --dry-run --json <<'EOF'
[{"op": "replace", "old": "\tdefaultBranch = main\n", "new": "\tdefaultBranch = master\n"}]
EOF
```

The last form reads the spec from stdin. One target per call: three files
are three calls and three commits, and a failure affects only its file.

**Templates.** `template: true` means the ops apply to the template's source
text, not to what lands; `chezmoi cat <target>` shows what lands.

**Encrypted files.** The source is ciphertext. The plaintext the anchors come
from is `chezmoi decrypt <source>`, and it holds secrets: read it only when
the user says to. The diff is masked unless `--reveal`.

## The ops reference

```json
{
  "skip_if_contains": "alias buccl=",
  "ops": [
    {"op": "insert_after", "anchor": "alias ssbrew='brew list | fzf'\n",
     "text": "\n# Upgrade Claude Code cask\nalias buccl='brew upgrade claude-code@latest'\n"}
  ]
}
```

| Op | Fields | Does |
|---|---|---|
| `replace` | `old`, `new` | replaces `old` with `new` |
| `delete` | `old` | removes `old` |
| `insert_after` / `insert_before` | `anchor`, `text` | puts `text` after / before `anchor` |
| `append` / `prepend` | `text` | adds `text` at the end (after a newline if the file lacks one) / start |
| `regex_replace` | `pattern`, `repl`, `flags` | Python `re.sub`; `flags` from `m`, `s`, `i`, `x`; `repl` uses `\1`, `\g<name>` |

- Ops apply in order, each to the previous one's result. Every field but
  `count` is a string.
- `count` (default 1) is an assertion: the match must occur exactly that many
  times, and all of them change. `"all"` means any number, at least one.
  `append` and `prepend` take no count.
- `skip_if_contains`: if the source already contains this string, nothing
  changes and the command exits 0 with `skipped: true`. Set it so a re-run is
  harmless.
- A bare list of ops is a spec with no `skip_if_contains`.

The common failures, all exit 1 with nothing written:

- `op N (replace): no match found (expected 1)`: the anchor isn't in the
  source byte for byte. Read the source again and copy it.
- `op N (replace): matched 2 time(s) but expected 1 - …`: make the match
  longer so it is unique, or set `count`.
- `ops produced no change to ~/.zshrc - the spec is probably wrong`: the
  result equals the source.

## New files

Write the file at its target path, then add it, dry run first:

```sh
cheztui add ~/.config/starship.toml --dry-run --json
cheztui add ~/.config/starship.toml --json
```

`--template` adds it as a template and `--encrypt` adds it encrypted, when the
user wants either. A directory is added with everything in it. A path chezmoi
already manages is refused: edit it with `cheztui edit` instead.

## Refusals

A refusal exits 1 with `{"error": "…"}` and changes nothing. cheztui's
refusals are its safety: do what each one asks, never look for a way past it.

- **Not managed** (`… isn't managed by chezmoi; use cheztui add to add it`):
  add it with `cheztui add`, or edit the file directly if it shouldn't be
  managed.
- **Drifted** (`… has drifted (MM); applying would overwrite it. …`): the
  target has a change of its own that the edit would overwrite. Show the user
  its `cheztui status` row and ask. Pass `--allow-drift` only when the user
  says to overwrite the target's change.
- **Dirty source tree** (`the source tree has uncommitted changes`, with a
  `stray` list): those files are the user's work in progress, which chezmoi's
  commit would sweep in. Name the `stray` files to the user and stop. Never
  commit, stash or delete them to get a clean tree.
- **chezmoi's `edit.command` is set**: chezmoi would open the user's editor
  instead of cheztui's. Tell the user; don't change their chezmoi config.
- **Not a regular file, an external, not UTF-8**: `cheztui edit` changes one
  text file. Tell the user.

Masked diffs (`********`) are expected for encrypted files and for files the
user's config always masks. Pass `--reveal` only when the user asks to see the
content: it puts their secrets into this transcript.

## Exit codes and JSON

| Exit | Meaning |
|---|---|
| 0 | done, or a dry run, or skipped (`skipped: true`) |
| 1 | refused, or the write failed or didn't verify |
| 2 | usage or config error |
| 3 | chezmoi is missing, older than 2.60, or failing |

`edit --json` gives `{target, source, template, encrypted, skipped, diff,
commit, pushed, verified}`; `add --json` the same without `skipped`, and
`source` is `null` on its dry run.

- `commit` is `{"sha", "subject"}`, or `null` when nothing was committed
  (chezmoi doesn't auto-commit, a dry run, a skip).
- `pushed` is `true` when the upstream has the commit, `false` when there is a
  commit it doesn't have (or no upstream), and `null` with no commit.
- `verified` is `true` when the target matches its source after the write,
  and `null` on a dry run or a skip.
- When the write runs but fails or doesn't verify, the same shape adds
  `"error"`, exit 1. Report it to the user as it stands.

A refusal is `{"error": "…"}`, plus `"stray": [path, …]` for a dirty tree.
Exit 2 and 3 print their message on stderr and nothing on stdout.
`cheztui: command not found` means cheztui isn't installed: Homebrew
(`brew install chocs-cat/tap/cheztui`) or PyPI (`uv tool install cheztui`).

## Beyond cheztui

Forgetting, renaming or removing a managed file, and changing its attributes,
aren't in cheztui's CLI yet: use chezmoi's own commands (`chezmoi forget`,
`chezmoi destroy`, `chezmoi chattr`; for a rename, `chezmoi forget` the old
path and `cheztui add` the new one). chezmoi may commit and push the whole source tree at once, so
first check the tree is clean, and stop on stray files as above:

```sh
git -C "$(chezmoi source-path)" status --short
```
