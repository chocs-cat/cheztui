# Decisions

Settled questions, so they aren't reopened by accident. Each entry says what
was decided and why, and is cited from the docs and the code as `(DEC-n)`.

- Add new entries at the end, numbered one past the greatest existing ID,
  with the month they were settled.
- To change one, discuss it first, then edit the entry in place (don't delete
  it): say what replaced it, when, and why, and update every doc and code site
  that cites it. Never give a changed decision a new ID.

Each entry has this shape:

```markdown
## DEC-1: <short title> (YYYY-MM)

What was decided and why, in a paragraph or two: the alternatives it rules
out and the reason they lost.

*Where:* the docs that carry the rule, if any.
```

## DEC-1: Drift first (2026-10)

The app opens on the Drift view. The question a user brings to cheztui is
what drifted and how to fix it, so the home view answers it. When nothing has
drifted, the Drift view shows one line: `✓ in sync`.

*Where:* `docs/spec.md` §3, §6 (Drift).

## DEC-2: One layout everywhere (2026-10)

Every view fills the same frame: header, view tabs, list on the left, detail
on the right, log line, key bar. A user learns the frame once and can move
between views without reorienting. Terminals narrower than 100 columns stack
the detail under the list.

*Where:* `docs/spec.md` §3, §4.

## DEC-3: Single-key actions, and a key bar of working keys (2026-10)

Actions are single letters on the selected row, with no nested menus. The key
bar lists only the keys that work in the current view at that moment. `:`
opens a command palette for everything else, and `?` shows full help.

*Where:* `docs/spec.md` §3, §5 (Keys).

## DEC-4: Preview, then one key (2026-10)

Any write opens a compact preview modal. Enter confirms and Esc cancels. The
user sees the exact command and its dry-run result before anything changes.

*Amended 2026-10:* interactive writes (edit and merge, which hand off to the
user's editor or merge tool) open no modal. There is no dry run to show, and
the editor is where the user reviews the change; a modal would only add a
keypress. They still run the pre-flight and show the result (DEC-8).

*Where:* `docs/spec.md` §3, §5 (Write flow).

## DEC-5: Show the result (2026-10)

After a write, the log line shows the outcome, the commit chezmoi made, and
whether chezmoi pushed it. chezmoi may auto-commit and auto-push, so the user
needs to see what reached the source repo.

*Where:* `docs/spec.md` §3, §5 (Write flow).

## DEC-6: Async, cached reads (2026-10)

Reads run asynchronously and are cached. The UI never blocks on chezmoi. Each
data source gets one chezmoi call per refresh, shared across views. Long
operations show progress in the log line and can be cancelled.

*Where:* `docs/spec.md` §3, §10.

## DEC-7: Quiet (2026-10)

No emoji and no banners. Each status has a glyph as well as a colour, so the
UI still works without colour.

*Where:* `docs/spec.md` §3.

## DEC-8: One write path (2026-10)

Every write (apply, re-add, add, merge, forget, destroy, chattr, ignore, state
reset, external refresh, edit, update) goes through the same four steps:

1. **Pre-flight.** When chezmoi's `git.autoCommit` is on, the source tree
   must be clean (`git status --porcelain` in the source dir). A dirty tree
   blocks the action, and the preview lists the stray files, since chezmoi's
   auto-commit would sweep them into its commit. The pre-flight runs before
   the preview's dry run too: chezmoi auto-commits a dirty tree even on a
   `--dry-run` of a command that writes the source.
2. **Preview modal.** The exact command, the targets, and a dry-run result
   (`chezmoi <cmd> --dry-run --verbose` where chezmoi supports it, else a
   computed diff). Batch actions get a per-file summary with expandable diffs.
3. **Confirm.** Enter runs; Esc cancels.
4. **Result.** The log line shows the outcome, the commit (from the source
   repo's `HEAD` before and after), and the push state. Views refresh.

A global `--dry-run` (`-n`) passes `-n` to every chezmoi call and marks the
header `DRY RUN`.

*Amended 2026-10:* the clean-tree block applies only when `git.autoCommit` is
on. With it off nothing sweeps stray files into a commit, and every write that
changes the source leaves the tree dirty, so blocking would stop the user
after each one. Instead the preview lists the uncommitted files as a warning,
and the result says the change is waiting uncommitted. The same amendment
lets interactive writes (edit, merge) skip steps 2 and 3 (DEC-4).
"autoCommit on" means chezmoi auto-commits: `git.autoCommit` or
`git.autoPush` (DEC-23).

*Where:* `docs/spec.md` §5 (Write flow), §8.

## DEC-9: Restore from a commit stays in the Repo view (2026-10)

The Repo view restores a file to a chosen commit's version. cheztui reads the
old source with `git show <commit>:<source-path>` and writes it through
`chezmoi edit --apply`, the same entry point `cheztui edit` uses. The restore
therefore gets the usual preview, apply and commit (DEC-8).

*Where:* `docs/spec.md` §6 (Repo).

## DEC-10: Masking by name patterns and token formats (2026-10)

cheztui masks secret-looking values in decrypted contents and rendered
templates. It detects them from password-manager template functions,
encrypted file bodies, keys matching configurable name patterns, and common
token formats (GitHub, `sk-…`, AWS access keys, Slack, PEM private-key
blocks). Two config glob lists adjust detection per file: `never_mask` for
files where the name patterns misfire, and `always_mask` for files masked
whole. Nothing decrypted reaches disk, the session log, or `--json` output
unless `--reveal` is passed.

*Where:* `docs/spec.md` §5 (Secrets), §9.

## DEC-11: `cheztui edit` takes one target per call (2026-10)

A failure then affects only that file, and each successful call makes one
commit. Agents call it once per file. Its ops spec stays compatible with the
`chezmoi-editor.py` script it replaces, so existing specs keep working.

*Where:* `docs/spec.md` §8.

## DEC-12: The Homebrew formula depends on chezmoi (2026-10)

`chocs-cat/tap/cheztui` declares `depends_on "chezmoi"`. cheztui does nothing
without the chezmoi binary.

*Where:* `docs/spec.md` §2; `scripts/formula.py`.

## DEC-13: Every read and write goes through the chezmoi binary (2026-10)

cheztui shells out to `chezmoi` and never parses or writes the source state
format itself. The exceptions read `.chezmoiignore` and `.chezmoiexternal.*`
for display. It prefers chezmoi's machine-readable output (`--format=json`,
`--path-style=all`) and parses text only where chezmoi has no JSON (`status`,
`doctor`).

*Where:* `docs/spec.md` §1 (Non-goals), §10.

## DEC-14: Drift direction from status plus the last-written hash (2026-10)

`chezmoi status` prints `MM` both when only the target changed and when both
sides did, so its columns alone can't tell `you→` from `both`. cheztui
classifies each status row like this:

- second column `R`: `run`;
- first column blank, second set: `src→`;
- first column set: `you→` when the target state's contents hash equals the
  `contentsSHA256` chezmoi recorded when it last wrote the entry (its
  `entryState`, from `chezmoi state dump --format=json`), else `both`.

The target-state contents come from `chezmoi dump --format=json` over the rows
whose first column is set; cheztui hashes them in memory and keeps nothing.
Rows whose second column is blank are not drift (chezmoi doesn't list them).

Each row has one default fix, run by `enter`: the first line of this table
that matches.

| Row | Default fix |
|---|---|
| `run` | apply (runs the script) |
| `src→` | apply |
| the target was deleted (first column `D`) | apply (restores it) |
| `you→`, a template | merge (`chezmoi re-add` skips templates without a word) |
| `you→`, a directory or symlink | apply, with a warning that it overwrites your change |
| `you→` | re-add |
| `both` | merge |

Comparing status with a re-render of the template each time was the
alternative; the recorded hash is what chezmoi itself uses to say "has changed
since chezmoi last wrote it", so cheztui agrees with chezmoi's own prompt.

*Amended 2026-10:* a deleted target's default fix was forget for `you→` rows.
It is now apply for every deleted target, which puts the file back. Forget
removes the file from the source repo, and with autoCommit and autoPush that
is committed and pushed at once; a file deleted by accident should come back
on `enter`, not vanish from the repo. Forget stays on `x`.

*Amended 2026-10 (#11):* "contents hash" is pinned per entry type. A file
hashes its contents; `dump` turns bytes that aren't UTF-8 into U+FFFD, so a
hash of the dumped string could never match a binary file's record, and such
rows hash the raw bytes from `chezmoi cat <target>` instead. A symlink hashes
its link target, which is what chezmoi records for it. A directory has no
contents and no recorded hash, so its permission bits are compared with the
recorded mode's. A target with no recorded state, a recorded type that
differs from the target state's, or no target-state entry is `both`: there
is nothing recorded for it to agree with. The directory-or-symlink line of the
table now covers `both` rows as well as `you→`, and rows with no
target-state entry (a `.chezmoiremove` target): `chezmoi merge` refuses
anything but a regular file ("not a file", observed with chezmoi 2.71) and
needs a source to merge from, so merge would be a default fix that always
fails, and apply with the overwrite warning is the one fix that works.

*Where:* `docs/spec.md` §6 (Drift).

## DEC-15: Row tags come from chezmoi, not from source filenames (2026-10)

`chezmoi managed --format=json` gives paths only. cheztui gets `tmpl`, `enc`
and `ext` from `chezmoi managed --include=templates|encrypted|externals`, and
`dir`, `link` and `exec` from the `type` and `perm` fields of
`chezmoi dump --format=json`. Decoding prefixes such as `encrypted_` or
`executable_` would parse the source state format, which DEC-13 rules out. The
one filename check left is `cheztui edit`'s `.tmpl` test on the source path.

*Where:* `docs/spec.md` §6 (Drift), §10.

## DEC-16: Apply and forget after a preview pass `--force`; apply-all ticks only safe rows (2026-10)

`chezmoi apply` on a target the user changed stops to ask "has changed since
chezmoi last wrote it", `chezmoi forget` asks before every removal, and with
`--no-tty` those prompts fail. The preview modal is cheztui's confirmation, so
apply and forget run with `--force`, for the dry run and for the real run. Because of that, `A` (apply all) lists every drift row
but ticks only `src→` and `run` rows; `you→` and `both` rows start unticked,
marked as overwriting the user's change, and the user ticks them on purpose.

*Where:* `docs/spec.md` §5 (Write flow), §6 (Drift).

## DEC-17: Masking ships with v0.1 (2026-10)

The first draft placed masking in v0.4 with the Secrets view. v0.1's diff pane
and template toggle would then show rendered secrets and decrypted files in
the clear, so masking (DEC-10) ships with v0.1 and the Secrets view stays in
v0.4. The rules that the first draft left open:

- The TUI masks every diff and rendered body it displays, whatever the file.
- A template whose source calls a password-manager function is masked whole:
  cheztui can't map a rendered line back to the call that produced it.
- `cheztui edit` and `cheztui add` mask their diff only for encrypted targets
  and `always_mask` files. Other sources are plain text the agent already
  read, and a masked diff would hide the change it is checking. `--reveal`
  shows the diff in the clear.
- A masked value prints as `********`, whatever its length.

*Where:* `docs/spec.md` §5 (Secrets), §8, §12.

## DEC-18: How `cheztui edit` hands the change to chezmoi (2026-10)

cheztui computes the new text in memory first: for an encrypted source it
reads the plaintext with `chezmoi decrypt`. It then runs
`chezmoi edit --apply --hardlink=false` with `cheztui _editor` as the editor.
The ops spec and the hash of the expected result reach `_editor` through a
temporary file (mode 0600, deleted afterwards) named by the
`CHEZTUI_EDIT_SPEC` environment variable. `_editor` re-applies the spec to the
file chezmoi hands it (plaintext, for an encrypted source) and refuses, writing
nothing, unless the result matches the expected hash, so what lands is what
the dry run showed. It writes in place, never by rename. `--allow-drift`
passes `--force`, since applying then overwrites the target's change.

*Where:* `docs/spec.md` §8.

## DEC-19: CLI contracts for v0.1 (2026-10)

- A global `--destination PATH` joins `--source` and `--chezmoi-config`, so
  tests and unusual setups can point every chezmoi call at one place.
- `cheztui add` takes one path per call, like `edit` (DEC-11), and refuses a
  path chezmoi already manages, pointing at `cheztui edit`.
- `pushed` is `true` when the upstream branch (`@{u}`) contains the new
  commit after the write, `false` when there is a commit that the upstream
  doesn't contain (or no upstream), and `null` when no commit was made.
  cheztui doesn't fetch to decide.
- `verified` comes from `chezmoi verify <target>`; the `verify` command's
  failing list comes from `chezmoi status` (rows whose second column is set,
  scripts included, as `chezmoi verify` counts them).
- The JSON shapes in `docs/spec.md` §7 and §8 are the contract; fields are
  only added, never renamed, before 1.0.

*Where:* `docs/spec.md` §7, §8.

## DEC-20: The TUI shows only views that exist (2026-10)

Until a view lands, its tab is absent and its number key does nothing. The
numbers stay as `docs/spec.md` §4 assigns them, so they don't move as views
arrive. Placeholder tabs would be keys that don't work, against DEC-3.

*Where:* `docs/spec.md` §4.

## DEC-21: chezmoi 2.60 is the floor (2026-10)

cheztui needs `managed --format=json --path-style=all`, `dump --format=json`,
`state dump --format=json`, `dump-config --format=json` and `edit
--hardlink`. It requires chezmoi 2.60 or later, checks `chezmoi --version`
once per run, and exits 3 (CLI) or shows the error (TUI) below it. Raise the
floor here when a feature needs a newer chezmoi, and raise CI's pinned
version with it (DEC-24).

*Amended 2026-10:* a version string that isn't `vX.Y.Z` passes the check. A
chezmoi built from source prints `dev`, and refusing it would lock out the
people most likely to run a new chezmoi; anything released prints a number.

*Where:* `docs/spec.md` §2.

## DEC-22: Adopt's `.chezmoiremove` key is `X` (2026-10)

`R` is the global reveal key (§5), and the Adopt detail pane shows a masked
preview, so Adopt can't also use `R` for "add a `.chezmoiremove` entry". The
action takes `X`: like destroy in the Files view, it makes chezmoi delete the
target, and the capital marks it as destructive. `R` reveals in every view.

*Where:* `docs/spec.md` §6 (Adopt).

## DEC-23: `git.autoPush` counts as autoCommit (2026-10)

chezmoi commits a write when `git.autoPush` is set even with
`git.autoCommit` off (observed with chezmoi 2.60 and 2.71: a `re-add` under
`autoPush = true` alone made a commit, then failed to push). Every rule that
asks whether chezmoi auto-commits (the clean-tree block of DEC-8, `cheztui
edit`'s step 4) therefore reads it as `git.autoCommit or git.autoPush`.
Reading `autoCommit` alone would let a dirty tree through to a commit that
sweeps the stray files in and pushes them. `dump-config --format=json` spells
the keys `git.autocommit` and `git.autopush`.

*Where:* `docs/spec.md` §5 (Write flow), §8.

## DEC-24: CI runs the integration tests against the floor chezmoi (2026-10)

The integration tests skip when `chezmoi` isn't installed, and CI's runners
don't have it, so they would never run there. The test job installs chezmoi
at the floor version from DEC-21 (2.60.0) on Linux and macOS. CI then proves
the floor is real, and a maintainer's machine, which runs a current chezmoi,
covers the newest release. Installing the latest in CI instead would leave
the floor untested; the parsers' recorded fixtures cover one fixed version
either way.

*Where:* `docs/spec.md` §11; `.github/workflows/ci.yml`.

## DEC-25: A failed chezmoi read exits 3 (2026-10)

When chezmoi itself fails a read cheztui needs (a template error makes
`chezmoi status` exit non-zero, say), the CLI exits 3, the code for chezmoi
missing or broken, and prints chezmoi's last error line on stderr. Exit 1
means a target failed or was refused, and for `verify` that the targets
differ; folding a failed read into it would let an agent take "couldn't
look" for "drifted". The read produced no result, so nothing goes on stdout,
with or without `--json`.

*Where:* `docs/spec.md` §7.
