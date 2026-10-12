# cheztui — project spec

> *chez toi*: "at your place." A clean, fast TUI for [chezmoi](https://www.chezmoi.io/).

Working title: **cheztui**. Status: draft spec, 2026-10-09.

## 1. What it is

cheztui is a terminal UI and CLI for managing dotfiles with chezmoi. It answers
the questions chezmoi's CLI makes awkward: *what drifted, which side changed,
and what is the right fix* — and makes the fix one keypress, with a preview.

It also replaces the `chezmoi-edit` agent skill with `cheztui edit`, a
scripted, assertion-checked edit command for agents and scripts. The repo
ships a skill that teaches agents to drive `cheztui edit`, as corral ships
`skills/corral`.

### Goals

- A very clean, efficient interface: one screen layout, few keys, no nested
  menus, and only information about the selected row.
- cheztui previews every write before running it.
- After a write, cheztui shows the result, including the commit chezmoi made.
- Expose the parts of chezmoi that are hard to see from the CLI: drift
  direction, script state, template data, externals, ignore rules, what a new
  machine would get.
- An agent-facing CLI (`--json`) that covers everything an agent needs,
  including safe edits, so the `chezmoi-edit` skill can be retired.

### Non-goals

- Replacing chezmoi. cheztui shells out to the `chezmoi` binary for every
  read and write; it never parses or writes the source state format itself,
  except where noted (reading `.chezmoiignore`/`.chezmoiexternal` for display).
- A git client. Repo features are read-mostly.
- Windows support.

## 2. Stack and shipping

Same shape as corral and tack.

| Item | Choice |
|---|---|
| Language | Python ≥ 3.11 |
| TUI | Textual (`>=8,<9`) |
| CLI | argparse, `--json` on every read command |
| Config | TOML via tomlkit, `~/.config/cheztui/config.toml` |
| Watching | watchfiles |
| Build | hatchling, uv, `uv.lock` |
| Checks | pytest + pytest-asyncio + pytest-textual-snapshot, ruff (corral's rule set), ty |
| Repo | `chocs-cat/cheztui`, default branch `master`, MIT |
| Releases | release-please, Conventional Commits, CI-gated `master` |
| Distribution | PyPI, Homebrew `chocs-cat/tap/cheztui` (`depends_on "chezmoi"`), Claude plugin manifest (`.claude-plugin/`) |
| Platforms | macOS and Linux |
| Requires | chezmoi ≥ 2.60, checked once per run; below it the CLI exits 3 and the TUI shows the error. A version string that isn't `vX.Y.Z` (a development build prints `dev`) passes (DEC-21) |

The repo starts private and goes public when the maintainer decides to publish
it. The logo is `docs/logo-light.svg` and `docs/logo-dark.svg`.

Repo layout follows corral: `AGENTS.md` (+ `CLAUDE.md` importing it),
`docs/decisions.md`, roadmap in GitHub Issues, `skills/cheztui/SKILL.md`.

## 3. Design principles

1. **Drift first.** The app opens on the Drift view. When nothing has
   drifted, the Drift view shows one line: `✓ in sync`.
2. **One layout everywhere.** Header · view tabs · list (left) · detail
   (right) · log line · key bar. Every view fills the same frame.
3. **Single-key actions.** Single-letter actions on the selected row. The key
   bar shows only keys that work *right now* in *this* view. `:` opens a
   command palette for everything else; `?` shows full help.
4. **Preview, then one key.** Any write opens a compact preview modal; Enter
   confirms, Esc cancels.
5. **Show the result.** After a write, the log line shows the outcome, the
   commit, and whether chezmoi pushed it.
6. **Fast.** Reads are async and cached; the UI never blocks on chezmoi.
   Long operations show progress in the log line and can be cancelled.
7. **Quiet.** No emoji, no banners. Each status has a glyph and a colour,
   so the UI works without colour.

## 4. Screen layout

```
cheztui • ~/.local/share/chezmoi                  ✓ verify   master ↑0 ↓0   clean
 1 Drift  2 Files  3 Scripts  4 Data  5 Externals  6 Adopt  7 Secrets  8 Health  9 Repo  0 Preview
─────────────────────────────────────┬──────────────────────────────────────────────
 you→ M  .zshrc                      │ ~/.zshrc                       you edited it
 src→ M  .gitconfig            tmpl  │ source  dot_zshrc
 both M  .claude/settings.json       │ ────────────────────────────────────────────
 src→ A  .config/ghostty/config      │ @@ -12,3 +12,4 @@
 run  R  once/install-brew.sh        │   alias ll='ls -la'
                                     │ + alias gs='git status'
─────────────────────────────────────┴──────────────────────────────────────────────
re-added ~/.zshrc → a1b2c3d "Update dot_zshrc" (pushed)
 a Apply  r Re-add  m Merge  e Edit  d Full diff  space Select  / Filter  ? Help
```

- **Header:** source path, verify badge, branch with ahead/behind,
  source-tree cleanliness (`clean` / `2 uncommitted`), dry-run indicator.
  The verify badge comes from the Drift data (`✓ verify` when no row has
  its second status column set, else `✗ verify`), not a separate
  `chezmoi verify` run. Ahead/behind compares `HEAD` with `@{u}` as last
  fetched; cheztui fetches only from the Repo view.
- **View tabs:** number keys switch; the current view is highlighted. A view
  with items needing attention shows a count (`1 Drift 4`). Only views that
  exist show a tab; a missing view's number does nothing, and the numbers
  never move (DEC-20).
- **List:** filterable (`/`), multi-select (`space`), vim and arrow keys.
- **Detail:** context for the selected row; scrolls independently
  (`J`/`K` or `PgUp`/`PgDn`).
- **Log line:** last result or current progress. `l` opens the full session
  log.
- **Key bar:** current-context keys only.

Narrow terminals (narrower than `ui.narrow_breakpoint`, default 100 cols)
stack detail under the list; `tab` toggles focus.

**Frame details** (DEC-33):

- **Header fields.** Left: `cheztui • <source>`, the effective source dir
  from `dump-config`, shown as `~/…` when it is under `$HOME` (as §7 shows
  targets). Right, three spaces apart: the verify badge; the branch,
  `<branch> ↑A ↓B` with an upstream, `<branch>` alone with none or before
  the first commit, `detached` when `HEAD` is detached; the cleanliness,
  `clean` or `N uncommitted` (the porcelain entries in the working tree);
  and `DRY RUN` under `-n`, in the warning colour. `ops.repo_state()`
  reads them: `dump-config`, then git's branch, ahead/behind and porcelain
  in its `workingTree`. A field is blank until its read succeeds, and
  blank again after its read fails.
- **Tab label.** `1 Drift`, plus ` N` when the Drift view has N rows.
- **Panes.** Wide: list and detail side by side, widths 2:3. Narrow
  (terminal width below `ui.narrow_breakpoint`, rechecked on resize): list
  above detail, equal heights. `tab` works only in the narrow layout; with
  the detail focused, `j`/`k` scroll it.
- **Key bar.** The current view's keys that work now, in the view's order;
  then `space Select` and `/ Filter` when the list has rows; then
  `? Help`. Every other key is in help and the palette, not the key bar.
- **Log line.** `reading chezmoi…` while a read runs; otherwise the last
  result, error or cancellation, empty at start. An error shows as
  `✗ <message>`, in the error colour, with the message the CLI would print
  after `cheztui: ` (§7).
- **List.** One row is current. `/` opens a one-line filter input under the
  list that filters as you type: case-insensitive substring of the target
  as displayed (`~/…`); `enter` returns to the list keeping the filter,
  `esc` clears it. `space` marks the current row selected (a `●` before
  it; unselected rows have a space there). Selection is a set of targets:
  it survives filtering, and a refresh keeps the targets still listed.
  Actions take the selected rows the filter shows. After a refresh the
  current row stays on its target if listed, else at the same position,
  clamped.

## 5. Global behaviour

### Keys (global)

| Key | Action |
|---|---|
| `1`–`9`, `0` | Switch view |
| `j`/`k`, `↑`/`↓` | Move |
| `J`/`K`, `PgUp`/`PgDn` | Scroll detail |
| `/` | Filter list (substring of the target path; `esc` clears) |
| `space` | Toggle selection (actions apply to selection, else to the current row) |
| `g` | Refresh |
| `:` | Command palette: every action in the current view and every global one, each with its key |
| `l` | Session log |
| `R` | Reveal masked values in the detail pane |
| `,` | Settings |
| `?` | Help: the current view's keys and the global ones, generated from the bindings |
| `q` | Quit |

The session log holds every result and error of the session, in memory only;
it is never written to disk.

**Reads in the TUI** (DEC-33):

- At start the TUI checks the chezmoi version (once per run, DEC-21), then
  runs the Drift read (`ops.drift`) and the header read
  (`ops.repo_state`) concurrently. `g` reruns both, and the version check
  too while it hasn't passed. A new read cancels one still running.
- `esc` on the main screen, with no input focused, cancels a read in
  progress: the earlier data stays and the log line says `refresh
  cancelled`.
- A failed read clears what it feeds: the header fields go blank, or the
  Drift list shows the error line in place of its rows and the tab drops
  its count. A failed version check leaves everything blank. The diagnostic
  is masked with the line-level rules and no `never_mask` exemption before
  the log line or session log sees it, as DEC-30 masks output that can't
  be assigned to a target.
- `q` quits from the main screen.

**Help, palette, session log:**

- `?` opens help: the current view's keys under the view's name (all of
  them, including ones the current row can't use), then the global keys
  under `Global` in the table's order, one `key  description` per line.
  Help, palette and key bar are generated from the same binding
  declarations. `esc`, `?` or `q` closes it.
- `:` opens the command palette with only cheztui's entries: the current
  view's actions, then the global ones, that work right now, each showing
  its key. Choosing one runs it as its key would. Textual's own system
  commands aren't listed, and no other key opens the palette.
- `l` opens the session log: entries oldest first, scrolled to the newest,
  each `HH:MM:SS  <log-line text>`; an error is followed by its whole
  masked diagnostic, indented two spaces. Every result, error and
  cancellation is logged. `esc`, `l` or `q` closes it.

### Write flow

Every write (apply, re-add, add, merge, forget, destroy, chattr, ignore,
state reset, external refresh, edit, update) goes through one path:

1. **Pre-flight.** When chezmoi auto-commits (`git.autoCommit` or
   `git.autoPush` is on, read from `chezmoi dump-config --format=json`:
   chezmoi commits when either is set, DEC-23), the source tree must be clean
   (`git status --porcelain` in the source dir). If it isn't, the preview
   says so and lists the stray files; the action is blocked until resolved
   (option: open the Repo view). The pre-flight runs before the preview's
   dry run, because chezmoi auto-commits a dirty tree even on a `--dry-run`
   of a command that writes the source. When chezmoi doesn't auto-commit, a dirty
   tree doesn't block: the preview lists the uncommitted files as a warning
   (DEC-8).
2. **Preview modal.** Shows the exact command, the targets, and a dry-run
   result (`chezmoi <cmd> --dry-run --verbose` where chezmoi supports it,
   otherwise a computed diff), masked (see Secrets). For batch actions, a
   per-file summary with expandable diffs. Apply and forget run with
   `--force`, in the dry run and for real: the modal is the confirmation, and
   without `--force` chezmoi stops to ask (about targets the user changed,
   and before every forget), which fails under `--no-tty` (DEC-16).
3. **Confirm.** Enter runs; Esc cancels.
4. **Result.** Log line shows the outcome and the commit chezmoi made (read
   from the source repo's `HEAD` before/after), and push state: `pushed`
   when `@{u}` contains the commit, `not pushed` otherwise, and `not
   committed` when the change is waiting in the source tree (DEC-19). On a
   failure the log line shows chezmoi's last error line and the session log
   the whole of it. Views refresh.

**Interactive writes** (edit and merge) hand off to the user's editor or
merge tool, so they skip steps 2 and 3: pre-flight, suspend, result (DEC-4,
DEC-8).

A read in progress can be cancelled (`esc`); a write can't, since chezmoi
may have committed part-way.

Global `--dry-run` (`-n`) mode passes `-n` to every chezmoi call and marks
the header `DRY RUN`.

**Preparation and execution** (`ops.py`, DEC-27). A prepared plan is data,
not authorization to bypass pre-flight later. It contains the action, exact
execution argv (displayed with shell quoting, executed without a shell),
targets, per-target previews, excluded targets with reasons, overwrite
warnings, the stray-file list and whether it is blocked. A blocked plan
contains no dry-run output: no writing chezmoi command has run. Read or git
failures while preparing propagate as errors; they never mean "clean".

- Each preparation reads effective `dump-config` and porcelain status in
  its `workingTree`, before any writing dry run. Execution reads them again
  after confirmation and before each command group; a newly dirty tree with
  auto-commit on blocks the remaining commands. A changed source,
  destination or working tree requires a new plan. With auto-commit off,
  the fresh stray list is a warning. This does not lock out external edits
  between the check and the command.
- Execution snapshots `HEAD` immediately before and after each command,
  even when the command fails. The result reports a commit only when the
  SHA changed (including an unborn HEAD becoming a commit); its SHA and
  subject come from git, and `pushed` uses `@{u}` without fetching (DEC-19).
  An unchanged HEAD gives `commit = null`, `pushed = null`. Remaining source
  changes say `not committed`; a clean, unchanged tree says `no commit`;
  a dry run says `dry run`.
- Each command result carries its action, targets, return code, masked
  output and error, commit and push state. A batch keeps the results in
  execution order. Stop at the first failure or pre-flight refusal, name
  the commands left unrun, and keep earlier results; batches have no
  rollback. Result collection failing after a command must say that the
  command ran and reporting failed, rather than claiming nothing changed.
- The front end refreshes after any execution attempt, including failure
  or interactive completion. It never executes a cancelled preview or an
  empty selection. cheztui serializes its own write attempts; cancellation
  of a waiting task must not kill a started write or abandon its result
  collection (§5, cancellation). Reads remain cancellable.

**Drift command plans** (DEC-28). Commands always name their targets: an
empty target list never becomes an unscoped chezmoi call.

| Action | Execution arguments after the global flags |
|---|---|
| apply | `apply --force --recursive=false <targets…>` |
| re-add | `re-add --recursive=false <targets…>` |
| forget | `forget --force <targets…>` |
| interactive edit | `edit --apply <target>` |
| interactive merge | `merge <targets…>` |

Apply is non-recursive so ticking a directory cannot apply unticked children.
Re-add accepts existing regular files with a source, excluding templates,
scripts and externals; directories, symlinks and deleted targets are excluded
as §6 describes. Merge accepts regular-file target-state entries with a
source and an existing regular destination file; unavailable rows are named
with a reason. Edit requires a source and a file or symlink that chezmoi can
edit. Forget requires a source and excludes externals, which chezmoi refuses.
Forgetting a directory also forgets its managed descendants: include them in
the preview, including entries without drift, and remove redundant descendant
arguments from the execution command. These checks use chezmoi metadata,
never decoded source prefixes (DEC-13, DEC-15).

- Explicit `a`, `r`, `x` and `M` use the selected rows (the current row
  when nothing is selected), deduplicated in Drift order. `e` and `m` use
  the current row; `M` is the selection merge. With `enter` on a selection,
  group default fixes in order apply, re-add, merge; targets within each
  group keep Drift order. A mixed selection has one preview for its
  non-interactive groups, with a note naming the later merge targets;
  merge then runs suspended, with its own fresh pre-flight and no dry-run
  merge. An all-interactive selection skips the modal (DEC-4).
- Apply-all lists every row, initially ticking exactly `src→` and `run`
  (DEC-16). Changing ticks rebuilds the command targets. Only ticked rows
  execute; warnings for applying `you→` or `both` rows don't depend on
  that row's default fix. This preserves DEC-14's classification even for
  a never-written target (#15 remains a design question).
- For each affected target, preview apply, re-add or forget using the same
  action flags plus `--dry-run --verbose --no-pager --color=false
  --use-builtin-diff`. A directory forget previews its affected descendants
  as well; its aggregate diff uses whole masking if any descendant needs
  it, and otherwise line-level masking without `never_mask`. Keep
  previews per target rather than guessing target boundaries in a combined
  diff. The execution remains one chezmoi call per action group. Preview
  failure blocks execution and reports the error; no partial preview is
  confirmable.
- Mask each preview before returning it to the front end, using its target,
  `enc` tag, and password-manager call detection on a template's source.
  Source text is read through chezmoi (`execute-template` with `include`
  of the source's absolute path, quoted as a Go string; `include` returns
  [literal contents](https://www.chezmoi.io/reference/templates/functions/include/)
  without rendering them). Rendered text and decrypted bodies stay in memory.
  Use the destination for globs (DEC-26).
  The diff path uses §5 Secrets' hunk rules, not body masking.

**Interactive dry runs** (DEC-29). Global `-n` never launches an editor or
merge tool: chezmoi's `edit --apply --dry-run` still edits the source.
Return the command that would run and a `dry run` result, with no commit or
push state, after pre-flight. There is no predicted diff for an edit the user
hasn't made. Normal interactive execution uses the same pre-flight and HEAD
result collection as other writes; the TUI only owns suspension and terminal
I/O. Every actual chezmoi invocation still goes through `chezmoi.py`.

**Diagnostics** (DEC-30). Command stdout and stderr are decoded with UTF-8
replacement and masked before entering a plan, result, exception used by a
front end, or session log. For output that cannot be assigned to one target,
mask it whole when any affected target needs whole masking (encrypted,
`always_mask`, password-manager template); otherwise apply the line-level
rules with no `never_mask` exemption. Keep the original last stderr line as
the source of the short error and mask it too; the log retains the full
masked diagnostic, so whole masking cannot hide whether the command failed.
If stderr is empty, use `chezmoi <action> exited <code>`. Raw subprocess
bytes are transient and never stored in front-end data. A TUI reveal changes
only its detail pane, never these stored diagnostics.
If preparation fails before masking metadata is known, mask its diagnostic
whole rather than returning potentially rendered secret text.

### Refresh

- Watches the source directory and the managed targets (via `watchfiles`) and
  refreshes the affected views, debounced.
- `g` forces a full refresh.
- Refreshes after every write.

### Secrets

- The TUI masks secret-looking values in every diff and rendered body it
  displays. Masking ships with v0.1 (DEC-17). Detection marks:
  - values from password-manager template functions: a template whose
    source calls one (`onepassword*`, `bitwarden*`, `pass`, `keyring`,
    `secret*`, `vault`, …) is masked whole, since a rendered line can't be
    mapped back to the call;
  - encrypted file bodies;
  - values under keys matching configurable name patterns
    (`token|secret|password|api_key`…);
  - values in common token formats, whatever their key is called: GitHub
    (`ghp_`, `gho_`, `github_pat_`), OpenAI/Anthropic-style `sk-…`, AWS
    access keys (`AKIA…`), Slack (`xox…`), and PEM private-key blocks.
- Two config glob lists adjust detection per file. `never_mask` turns
  off the line-level detection (name patterns and token formats) for files
  where it misfires (a keybindings file full of `key = …`); an encrypted
  file or password-manager template in it is still masked whole.
  `always_mask` masks the whole body of files like `.netrc`, `.env` or
  `~/.ssh/id_*`, and wins over `never_mask` (DEC-26).
- A masked value prints as `********`, whatever its length.
- `R` reveals for the current detail view only; moving away re-masks.
- Nothing decrypted is ever written to disk, the session log, or `--json`
  output (unless `--reveal` is passed explicitly on the CLI).
- `cheztui edit` and `cheztui add` mask their diff only for encrypted
  targets and `always_mask` files; other sources are plain text the agent
  already read (§8, DEC-17).

**Detection rules** (`secrets.py`). One call masks one target's body or
diff.

- **Whole.** A body is masked whole when the target is encrypted, matches
  `always_mask`, or is a template whose source calls a password-manager
  function. Every line with content becomes `********`; an empty line stays
  empty. The CLI masks only for the first two reasons (DEC-17).
- **Password-manager functions.** A template calls one when its source has
  one of these names as a whole word, either right after a `|` (spaces
  allowed) or followed by whitespace and the start of an argument (`"`,
  `` ` ``, `$`, `(`, `.`): `onepassword…`, `bitwarden…`, `lastpass…`,
  `keepassxc…`, `keeper…`, `dashlane…`, `doppler…`, `gopass…`, `rbw…`,
  `protonPass…`, `awsSecretsManager…`, `hcpVaultSecret…`, `ejsonDecrypt…`
  (each with any letters after it), and `azureKeyVault`, `pass`,
  `passFields`, `passRaw`, `passhole`, `secret`, `secretJSON`, `keyring`,
  `vault`, `decrypt`. The call form keeps a shell `pass` or a word in a
  comment from masking a template; template delimiters aren't parsed.
- **Line level** (the TUI only; off for `never_mask` files). Each line is
  checked on its own, and the spans found are merged, each run becoming one
  `********`:
  - *Name patterns.* A line `KEY = VALUE` or `KEY: VALUE`: optional
    indentation, then optionally `export ` or `- `, then KEY (letters,
    digits, `_`, `.`, `-`, optionally in single or double quotes), then
    `=` or `:` with optional spaces around it. When any `mask_patterns`
    entry matches KEY (`re.search`, ignoring case), the span is the inside
    of a quoted VALUE (up to the closing quote, or the end of the line), or
    else the rest of the line less trailing whitespace and a trailing `,`.
    A value that is empty, `{` or `[` is left alone.
  - *Token formats* (when `token_formats` is on), each not preceded by a
    letter, digit or `_`: `gh[pousr]_[A-Za-z0-9]{20,}`,
    `github_pat_[A-Za-z0-9_]{20,}`, `sk-[A-Za-z0-9_-]{20,}`,
    `(AKIA|ASIA)[A-Z0-9]{16}` not followed by a letter or digit,
    `xox[abposr]-[A-Za-z0-9-]{10,}`.
  - *PEM blocks* (also under `token_formats`). From a line containing
    `-----BEGIN … PRIVATE KEY-----` to the next line containing
    `-----END … PRIVATE KEY-----`, every line between becomes `********`
    (the two marker lines stay); with no END line, to the end of the body.
- **Diffs.** In a unified diff only hunk lines are masked: each keeps its
  first character (` `, `+`, `-`) and the rest is masked as a line of the
  body. A hunk's extent comes from the counts in its
  `@@ -a[,b] +c[,d] @@` line (a missing count is 1; an empty line inside
  a hunk is an empty context line). Lines outside hunks (`diff`, `index`,
  `---`, `+++`, mode lines, `@@` lines, `\ No newline at end of file`)
  pass through. An open PEM block runs on across hunks and ends at a
  `diff ` line outside a hunk.
- **Globs** (`never_mask`, `always_mask`) are matched against the target's
  absolute path, case-sensitively. A leading `~/` is the destination
  directory (`$HOME` unless `--destination` says otherwise, DEC-26); a
  pattern that starts with neither `~/` nor `/` is relative to it. `*` and
  `?` don't match `/`, and match a leading `.`; `[…]` is a character class;
  `**` as a whole path segment matches zero or more segments, so `a/**`
  matches `a` itself and `**/.env` matches `~/.env`.
- Line endings are kept, and a body or diff with nothing to mask comes back
  unchanged.

### External editor and pager

- `e` suspends the TUI and runs `chezmoi edit --apply <target>` with the
  user's `$VISUAL`/`$EDITOR`, then resumes and shows the result.
- `d` suspends and runs `chezmoi diff <target>` with the user's configured
  diff pager.

## 6. Views

Each view lists: purpose · list rows · detail pane · keys · chezmoi calls.

### 1 · Drift

The home view. Everything where actual state ≠ target state, or a script
would run.

- **Rows** come from `chezmoi status` (both columns), joined with
  `chezmoi managed --format=json --path-style=all` for source paths. Each
  row gets a **direction** (DEC-14):
  - `you→`: the target changed since chezmoi last wrote it (you edited the
    file), and the source didn't.
  - `src→`: only the source changed (pull, edit).
  - `both`: both changed.
  - `run`: a script will run on next apply.

  `chezmoi status` prints `MM` for both `you→` and `both`, so a row whose
  first column is set is `you→` when the sha256 of its target-state contents
  (`chezmoi dump --format=json` over those rows) equals the `contentsSHA256`
  in its `entryState` (`chezmoi state dump --format=json`), and `both`
  otherwise. A first column that is blank makes the row `src→`; a second
  column `R` makes it `run`. The contents are hashed in memory and dropped.
  What is compared depends on the target-state entry's `type` (DEC-14):
  - a file hashes its contents (the dumped `contents` as UTF-8, empty when
    absent). `dump` turns bytes that aren't UTF-8 into U+FFFD, so when the
    dumped contents hold U+FFFD the bytes come from `chezmoi cat <target>`
    instead, one call per such row (#11);
  - a symlink hashes its `linkname`, which is what chezmoi records;
  - a directory has no contents and no recorded hash: its `perm` is compared
    with the recorded `mode`'s permission bits (`mode & 0o777`);
  - no `entryState` for the target, a recorded `type` that differs from the
    target state's, or no target-state entry at all makes the row `both`.

  Rows come from `status` alone; a row whose second column is blank isn't
  drift and is dropped. A row's `source` is the `sourceAbsolute` of its
  `managed` entry, matched on the absolute target; there is none when the
  target isn't in `managed` or its `sourceRelative` is empty (as for a
  `.chezmoiremove` target). With no status rows, nothing else is read. Each
  refresh otherwise reads `status`, `managed` with each of the three
  `--include` filters below and without one, `dump` over the status rows'
  targets, `state dump`, and `chezmoi cat` for any U+FFFD row.
- **Default fix**, run by `enter`: the first line that matches (DEC-14).

  | Row | Default fix |
  |---|---|
  | `run` | apply (runs the script) |
  | `src→` | apply |
  | target deleted (first column `D`) | apply (restores it; `x` forgets it instead) |
  | `you→`, a template | merge (`chezmoi re-add` skips templates) |
  | `you→` or `both`, a directory, a symlink, or no target-state entry | apply, warning that it overwrites your change (`chezmoi merge` needs a file on both sides) |
  | `you→` | re-add |
  | `both` | merge |

- Row tags: `tmpl`, `enc`, `exec`, `link`, `ext`, `dir`, always in that
  order. `tmpl`, `enc` and `ext` come from
  `chezmoi managed --include=templates|encrypted|externals`; `dir`, `link`
  and `exec` from the `type` and `perm` of `chezmoi dump --format=json`
  (`dir` for type `dir`, `link` for `symlink`, `exec` for a `file` whose
  `perm` has an execute bit). cheztui never decodes source filename
  prefixes (DEC-13, DEC-15).
- Sort: `both` first, then `you→`, `src→`, `run`; within each, by absolute
  target path in code-point order.
- **Detail:** target and source paths, direction in words, built-in
  syntax-highlighted unified diff (via `chezmoi diff <target>` captured with
  no pager, rendered by cheztui). For templates, a toggle (`t`) between the
  diff and the rendered output.
  The diff and the rendered output are masked (§5, Secrets); `R` reveals.
- **Keys:** `enter` default fix for the row · `a` apply · `r` re-add (not
  on templates, directories, symlinks or deleted targets: the key bar hides
  it there) · `m` merge (suspends for chezmoi's merge tool; hidden on
  directories, symlinks and rows with no target-state entry, which
  `chezmoi merge` refuses) · `M` merge
  through the selection, one `chezmoi merge` call · `e` edit source · `d`
  full diff · `x` forget · `A` apply all.
- **Selection:** with rows selected, `a`, `r`, `x` and `M` act on every
  selected row in one chezmoi call, after one preview listing each file;
  rows the action can't take are left out and named in the preview.
  `enter` runs each selected row's default fix, one call per action, under
  one preview.
- **Apply all** (`A`): the preview lists every drift row. `src→` and `run`
  rows start ticked; `you→` and `both` rows start unticked and marked as
  overwriting your change. `space` ticks and unticks (DEC-16).

### 2 · Files

All managed entries, as a collapsible tree of targets.

- **Rows:** `chezmoi managed --format=json --path-style=all`, tree-shaped,
  with drift markers from the Drift view and attribute tags.
- **Detail:** target/source paths, attributes (template, encrypted,
  executable, private, readonly, exact, empty, symlink, run-once…), size,
  last commit touching the source file.
- **Keys:** `space` expand/collapse · `e` edit · `v` view (`chezmoi cat`,
  masked) · `c` chattr (attribute picker, pre-filled from current state) ·
  `x` forget · `X` destroy (typed confirmation) · `i` add an ignore rule ·
  `o` open the source file in `$EDITOR` without apply (palette only).
- chezmoi's own special files (`.chezmoiignore`, `.chezmoiremove`,
  `.chezmoiexternal.*`, `.chezmoidata/`, `.chezmoitemplates/`,
  `.chezmoiscripts/`, `.chezmoiversion`, config template) appear in a
  `chezmoi` group at the top.

### 3 · Scripts

The script ledger: every `run_` script and whether it will run.

- **Rows:** scripts from `chezmoi managed --include=scripts --format=json`,
  joined with `chezmoi state dump --format=json` (`scriptState` for
  `run_once_`, `entryState` for `run_onchange_`). Columns: type
  (`always`/`once`/`onchange`), phase (`before`/`after`), last run time,
  will-run-next-apply flag (from `status` `R`).
- **Detail:** rendered script (`chezmoi cat`), stored hash vs current hash,
  last-run timestamp.
- **Keys:** `enter`/`v` view · `e` edit · `u` reset state so the script runs
  on next apply (`chezmoi state delete --bucket=… --key=…`; preview shows
  the bucket and key) · `U` reset all once-scripts (typed confirmation) ·
  `!` run now (applies just that script).

### 4 · Data & Templates

The template workbench.

- **Sub-tabs** (`[`/`]`): **Data** · **Templates** · **Config**.
- **Data:** `chezmoi data --format=json` as a collapsible tree; values from
  `.chezmoidata` files marked with their source file. `y` copies the template
  expression for the selected key (`{{ .chezmoi.os }}`).
- **Templates:** every `.tmpl` source plus `.chezmoitemplates/` partials.
  Detail shows source on top, rendered output below
  (`chezmoi execute-template --file`), re-rendered live when the file
  changes. `p` opens a scratch prompt to evaluate any template expression.
- **Machine overrides:** `o` opens an override panel to render as another
  machine: override `.chezmoi.os`, `.chezmoi.hostname`, and prompt answers
  (`--init --promptString/--promptBool/--promptChoice`). Overrides are named
  profiles stored in cheztui's config; the header shows the active profile.
- **Config:** `chezmoi cat-config` (masked) and the config template if one
  exists. `e` runs `chezmoi edit-config` or `edit-config-template`.

### 5 · Externals

- **Rows:** entries from `.chezmoiexternal.*` (parsed for display via
  `chezmoi dump --format=json` of external targets where possible): target,
  type (`file`/`archive`/`git-repo`), URL, refresh period, age of cached
  copy (from chezmoi's cache dir mtime), outdated flag.
- **Detail:** full entry, including include/exclude filters and checksum.
- **Keys:** `f` refresh selected (`chezmoi apply --refresh-externals=always
  <target>`) · `F` refresh all · `e` edit the externals file.

### 6 · Adopt

Bring files under management, or decide to ignore them.

- **Rows:** `chezmoi unmanaged` under configured roots (default
  `~/.config`, `~/.local/bin`, `~/.ssh`; set in config), as a tree. A second
  section lists `chezmoi ignored` entries, each with the `.chezmoiignore`
  rule that matched (cheztui renders `.chezmoiignore` with
  `execute-template` and matches the patterns itself; best-effort).
- **Detail:** file preview (masked), size, mode.
- **Keys:** `a` add (flag picker: template, encrypt, exact, follow,
  recursive, create, empty) · `i` ignore (writes a rule to `.chezmoiignore`
  through `cheztui edit`, with a preview) · `X` add a `.chezmoiremove` entry,
  so chezmoi deletes the file on every machine (as Files' `X` destroys;
  `R` stays the global reveal, DEC-22) · `u` un-ignore (removes the matching
  rule, with preview).

### 7 · Secrets & Encryption

- **Rows:** encrypted source files; templates that call secret functions
  (`onepassword*`, `bitwarden*`, `pass`, `keyring`, `secret*`, `vault`, …,
  found by scanning template sources); each password manager's status
  (installed, signed in/unlocked) where its CLI can report it.
- **Detail:** encrypted files show decrypted content in memory, masked;
  templates show which lines call which manager.
- **Keys:** `R` reveal · `e` edit encrypted (`chezmoi edit` handles
  decrypt/re-encrypt) · `c` toggle encryption (`chattr +encrypted/-encrypted`)
  · `k` keyring get/set (`chezmoi secret keyring`) · palette: generate an
  age key (`chezmoi age-keygen`) and show the config snippet to use it.

### 8 · Health

- **Cards:** `chezmoi doctor` parsed into pass/warn/fail/info rows;
  `chezmoi verify` result; chezmoi version vs latest.
- **Keys:** `enter` show the full doctor line · `U` upgrade chezmoi
  (`chezmoi upgrade`, or the Homebrew command when installed by brew).

### 9 · Repo

The source repository, read-mostly.

- **Rows:** recent commits (`git log` in the source dir), newest first,
  with files changed.
- **Header detail:** branch, ahead/behind (background `git fetch` on start),
  uncommitted files (these block writes; see §5).
- **Detail:** the commit's diff, by target name.
- **Keys:** `p` pull + apply (`chezmoi update`; preview runs
  `git fetch` + shows incoming commits and the apply dry-run) · `P` push
  (`chezmoi git push`) · `o` open lazygit in the source dir (suspends) ·
  `v` restore a file to the selected commit's version. cheztui reads the old
  source content with `git show <commit>:<source-path>` and writes it
  through `chezmoi edit --apply`, using the same editor entry point as
  `cheztui edit`, so the restore gets the usual preview, apply and commit.

### 0 · Preview

What a new machine would get.

- **Temp dir (always available):** run `chezmoi apply --destination
  <tmpdir> --dry-run`/`dump --format=json`, optionally with a machine
  override profile from the Data view. List shows every target that would be
  created, with scripts that would run; detail shows the rendered file
  (masked).
- **Docker** (when `docker` is on PATH and configured): `chezmoi docker run
  <image>` with the configured image list; output streams into the detail
  pane.
- **SSH** (when hosts are configured): `chezmoi ssh <host>`; same streaming
  output.
- Temp dirs are always cleaned up.

## 7. CLI

`cheztui` with no arguments opens the TUI. Every read command takes
`--json` (result on stdout, progress on stderr). Exit codes: `0` ok,
`1` some target failed / refused, `2` usage or config error, `3` chezmoi
missing or broken, or a chezmoi read failed (DEC-25).

With no arguments on a terminal, cheztui loads its config first; a config
error, or a `ui.theme` Textual doesn't know (`<path>: ui.theme '<name>' is
not a Textual theme`), prints `cheztui: <message>` on stderr and exits 2
without starting the TUI. The global flags reach every chezmoi call the TUI
makes, and the TUI checks the chezmoi version itself (§5, Reads in the TUI).
Without a terminal it prints the help.

Every command loads cheztui's config (§9) first, then checks the chezmoi
version (DEC-21). A config error (exit 2) or a chezmoi error (exit 3) prints
`cheztui: <message>` on stderr and nothing on stdout, with or without
`--json`; for a failed chezmoi command the message ends with chezmoi's last
error line.

| Command | Purpose |
|---|---|
| `cheztui status [--json]` | Drift rows with direction and default fix |
| `cheztui files [--json]` | Managed entries with attributes |
| `cheztui scripts [--json]` | Script ledger |
| `cheztui scripts reset <script>` | Reset run state (prompts unless `--yes`) |
| `cheztui data [--json] [--profile NAME]` | Template data |
| `cheztui render <target> [--profile NAME]` | Rendered output, masked unless `--reveal` |
| `cheztui externals [--json]` | Externals and age |
| `cheztui unmanaged [PATH…] [--json]` | Adopt candidates |
| `cheztui verify [--json]` | Pass/fail, failing targets |
| `cheztui doctor [--json]` | Parsed doctor results |
| `cheztui preview [--profile NAME] [--json]` | Temp-dir new-machine preview |
| `cheztui edit <target> --ops FILE\|- [--dry-run] [--allow-drift] [--reveal] [--json]` | Safe scripted edit (§8) |
| `cheztui add <path> [--template] [--encrypt] [--dry-run] [--reveal] [--json]` | Bring a new file under management (§8) |

Global flags: `--dry-run/-n`, `--chezmoi-config PATH`, `--source PATH`,
`--destination PATH` (DEC-19), and `--profile NAME` from v0.3. Each is
passed through to every chezmoi call, as given. They go before the command
(`cheztui -n status`), so they never clash with a command's own flags
(`edit --dry-run`).

`--json` shapes are a contract: before 1.0, fields are only added, never
renamed (DEC-19).

- `status --json`: `{"rows": [{"target", "source", "direction",
  "default_fix", "columns", "tags"}]}`. `target` is absolute; `source` is
  the absolute source path, or `null` when the source no longer has the
  entry (§6 Drift); `direction` is `you`, `src`, `both` or `run`;
  `default_fix` is `apply`, `re-add` or `merge`; `columns` is chezmoi's two
  status characters (`"MM"`, `" R"`); `tags` is a list from `tmpl`, `enc`,
  `exec`, `link`, `ext`, `dir`, in that order. Rows are in the Drift view's
  sort order. Exit 0 whether or not anything drifted.
- `verify --json`: `{"verified": bool, "failing": [target, …]}`, where
  `failing` is every status row whose second column is set, scripts
  included, as absolute targets in the Drift view's sort order. Exit 0 when
  verified, 1 when not.

Without `--json`, `status` prints one line per row in the same order: the
direction as the Drift view shows it (`both`, `you→`, `src→`, `run`), the
two status columns, the target, the default fix and the tags (space
separated), in columns two spaces apart, padded to the widest value and
with trailing spaces stripped. A target under `$HOME` shows as `~/…`. With
no rows it prints `✓ in sync`. `verify` prints `✓ verify`, or `✗ verify`
followed by one failing target per line, indented two spaces, in the same
`~` form.

```
both  MM  ~/.claude/settings.json  merge
you→  MM  ~/.zshrc                 re-add
src→   M  ~/.gitconfig             apply   tmpl
run    R  ~/install-brew.sh        apply
```

Behaviour lives in `ops.py`; the CLI and the TUI both call it.

## 8. `cheztui edit` and the agent skill

Absorbs the `chezmoi-edit` skill and its `chezmoi-editor.py` script.

### Command

```sh
cheztui edit ~/.zshrc --ops /path/ops.json --dry-run   # print unified diff, write nothing
cheztui edit ~/.zshrc --ops /path/ops.json             # apply, commit (via chezmoi), verify
cheztui edit ~/.zshrc --ops -                          # spec on stdin
```

It takes one target per call, as today's script does. A failure affects
only that file, and each successful call produces one commit. Agents call
it once per file. The target has `~` expanded and is made absolute against
the working directory, without resolving symlinks. The spec (`--ops FILE`,
or `-` for stdin) is read as UTF-8 and parsed as JSON before any chezmoi
call; a spec that can't be read or parsed is a refusal (exit 1).

It runs the skill's whole procedure as one command, refusing at the first
step that fails:

1. **Managed?** `chezmoi managed --format=json --path-style=all <target>`.
   Not managed (no entry whose absolute path is the target) → exit 1 with a
   message pointing at `cheztui add`. When `managed` rejects the target,
   the effective destination from `dump-config` decides: a target outside
   it is not managed (the same refusal); inside it, the failure exits 3
   (DEC-32). The same
   read with `--include=files`, `templates`, `encrypted` and `externals`
   gives the entry's kind and attributes (DEC-15). An external, or a target
   that isn't a regular file (a directory, symlink or script), is refused:
   `cheztui edit` changes one file's text, and applying an edited script
   would run it (DEC-31).
2. **Template?** If chezmoi lists it as a template, say so in the output
   (and in `--json`), since the applied result differs from the edit.
3. **Drift?** If `chezmoi status <target>` is non-empty, refuse (exit 1):
   applying would clobber the target. `--allow-drift` overrides, and passes
   `--force` so the apply overwrites the target's change.
4. **Pre-flight.** The write path's pre-flight (§5). Refuse if chezmoi's
   `edit.command` is set: chezmoi then runs that editor and ignores
   `$EDITOR`, so step 7 can't hand the change over (DEC-31). When chezmoi
   auto-commits (§5, DEC-23), refuse if `git status --porcelain` in the
   source dir is non-empty, listing the stray files. With it off, warn and
   go on (DEC-8).
5. **Apply ops** to the source text in memory, with the rules below; on any
   failure, exit 1 having written nothing. The text is the source's exact
   bytes, decoded as UTF-8 (a source that isn't UTF-8 is refused), with its
   line endings kept: read through chezmoi with `execute-template` and
   `include` (§5), or as plaintext with `chezmoi decrypt <source>` for an
   encrypted one.
6. **`--dry-run`** (or the global `-n`) prints the unified diff and stops.
   The diff is masked for encrypted targets and `always_mask` files unless
   `--reveal` is passed (DEC-17).
7. **Write** by running `chezmoi edit --apply --hardlink=false [--force]
   <target>` with cheztui itself as `$EDITOR` and `$VISUAL` (an internal
   `cheztui _editor` entry point that writes in place, never renames; the
   value is the running interpreter's `python -P -m cheztui _editor`,
   shell-quoted, which chezmoi splits as a shell would, so PATH doesn't
   matter; `-P` keeps the working directory off `sys.path`, so a module
   there can't shadow one cheztui imports, DEC-18). The
   ops spec and the expected result's sha256 reach `_editor` in a 0600
   temporary file named by `CHEZTUI_EDIT_SPEC`, deleted afterwards.
   `_editor` re-applies the spec to the file chezmoi hands it and writes
   nothing unless the result matches that hash, so what lands is what the
   dry run showed (DEC-18). The command goes through the write path: a
   fresh pre-flight first, `HEAD` before and after (§5, DEC-27).
8. **Verify:** `verified` is true when the source, read again as in step 5,
   gives the expected hash and `chezmoi verify <target>` exits 0. Report the
   commit made and push state.

**The diff** is a unified diff of the source text with three lines of
context, headed `--- a/<target>` and `+++ b/<target>`, the target relative
to the destination. A line without a final newline is followed by
`\ No newline at end of file`, so every hunk's line counts hold (DEC-31).

**`_editor`** (`cheztui _editor <path>`) is dispatched before argument
parsing: it isn't in `--help`, and it loads no config and runs no chezmoi.
It takes exactly one path, reads `CHEZTUI_EDIT_SPEC` (JSON:
`{"spec": <the ops spec as given>, "sha256": "<hex>"}`), reads the file's
bytes as UTF-8, applies the spec with the same engine, and writes the result
in place only when its sha256 (of the UTF-8 bytes) is the expected one; a
skip counts as a mismatch. It exits 0 when written and 1 when refused, with
its message on stderr as `cheztui _editor: <message>`; a non-zero exit makes
chezmoi abort without applying.

### Ops spec

Compatible with `chezmoi-editor.py`, so existing specs keep working:

```json
{
  "skip_if_contains": "alias buccl=",
  "ops": [
    {"op": "insert_after", "anchor": "alias ssbrew='brew list | fzf'\n",
     "text": "\n# Upgrade Claude Code cask\nalias buccl='brew upgrade claude-code@latest'\n"}
  ]
}
```

Ops: `replace` (`old`/`new`), `delete` (`old`), `insert_after` /
`insert_before` (`anchor`/`text`), `append` / `prepend` (`text`),
`regex_replace` (`pattern`/`repl`, `flags` from `msix`). `count` defaults
to 1 and is an assertion (`"all"` = any number ≥ 1). A bare list of ops is
accepted. `skip_if_contains` makes a re-run a no-op (exit 0).

The rules, in the script's order:

- The spec is a JSON object, or a list taken as `{"ops": <list>}`; `ops`
  must be a non-empty list. Unknown keys are ignored.
- `skip_if_contains`, when it is a non-empty string the source contains,
  skips before any op is looked at. It must be a string when present and
  not `null`.
- Ops apply in order, each to the previous one's result. Each op is an
  object with an `op` name; a needed field that is missing or `null`, or
  given but not a string, is an error.
- `replace`, `delete`, `insert_after` and `insert_before` count the
  non-overlapping occurrences of `old` or `anchor` (Python's `str.count`)
  and replace that many, or all of them for `"all"`; `insert_after` puts
  `text` after each occurrence, `insert_before` before it.
- `append` adds a newline first when the text is non-empty and doesn't end
  with one, then `text`; `prepend` puts `text` first. Neither checks a
  count.
- `regex_replace` compiles `pattern` with Python's `re` and the `flags`
  letters (`m`, `s`, `i`, `x`), counts `findall` matches and substitutes
  with `re.sub` (Python's replacement syntax, `\1`, `\g<name>`).
- `count` is a positive integer or `"all"`; a boolean is not an integer.
- When every op has run, a result identical to the source is an error.

Messages, with `op N (<name>)` labelling op N (from 1) and `<target>` the
target in `~` form:

| Case | Message |
|---|---|
| not JSON | `edit spec is not valid JSON: <error>` |
| wrong top-level type | `edit spec must be a JSON object or list of ops` |
| no ops | `edit spec has no 'ops' list` |
| op not an object | `op N is not an object` |
| field missing | `op N (replace): needs 'old' and 'new'` (likewise `'old'`, `'anchor' and 'text'`, `'text'`, `'pattern' and 'repl'`) |
| field not a string | `op N (replace): 'old' must be a string` |
| bad `count` | `op N (replace): 'count' must be a positive integer or "all"` |
| no match, `"all"` | `op N (replace): no match found` |
| no match | `op N (replace): no match found (expected 1)` |
| wrong number | `op N (replace): matched 2 time(s) but expected 1 - make the match string more specific, or set "count"` |
| bad flag | `op N (regex_replace): unknown regex flag 'q' (use m, s, i, x)` |
| bad regex | `op N (regex_replace): bad regex: <error>` |
| bad replacement | `op N (regex_replace): bad replacement: <error>` |
| unknown op | `op N (frob): unknown op 'frob'` |
| no change | `ops produced no change to <target> - the spec is probably wrong` |

**Output.** Human output is the diff on stdout, then one result line in the
TUI's log-line form; notes, warnings and refusals go to stderr.

| Case | stdout result line |
|---|---|
| committed | `edited ~/.zshrc → a1b2c3d "Update .zshrc" (pushed)`, or `(not pushed)` |
| no commit | `edited ~/.zshrc (not committed)`, or `(no commit)` |
| dry run | `would edit ~/.zshrc (dry run)` |
| skipped (no diff) | `skipped ~/.zshrc: already contains 'alias buccl=', leaving unchanged` |

The sha is seven characters; the subject is git's, in double quotes. A
template adds `note: ~/.zshrc is a template; the applied result differs
from the edit` on stderr; stray files with auto-commit off add `warning:
uncommitted files in the source tree:` and one indented path per line. A
refusal prints `cheztui: <message>` and, for a dirty tree, the stray files
indented two spaces.

`--json` output: `{target, source, template, encrypted, skipped, diff,
commit, pushed, verified}`. `target` and `source` are absolute; `diff` is
the unified diff (masked as above), `""` when skipped; `commit` is
`{"sha", "subject"}` (the full sha) or `null`; `pushed` is `true`, `false`
or `null` (DEC-19); `verified` is `null` on a dry run or a skip. A refusal
exits 1 with `{"error": "…", "stray": [path, …]}` on stdout, `stray` only
for a dirty tree. Warnings and notes go to stderr in JSON mode too.

**Failures after the write starts.** When the chezmoi command fails, or
runs but the target doesn't verify, the exit is 1: `--json` prints the
success shape plus `"error"`, and human output prints chezmoi's masked
stderr, then `cheztui: <error>`. The error is the last `cheztui _editor:`
line in chezmoi's stderr when there is one, else the write path's short
error (§5, Diagnostics); for a verify failure, `<target> doesn't verify
after the edit`. Exit codes otherwise follow §7: 2 for usage and config
errors, 3 when chezmoi is missing or broken or a chezmoi read fails; a
pre-flight git failure is a refusal (`pre-flight failed: …`).

### New files

`cheztui add <path> [--template] [--encrypt] [--dry-run]` wraps
`chezmoi add` with the same clean-tree check and result report, so the
skill has one tool for both cases. It takes one path (a file, or a
directory added recursively), normalized as `edit`'s target, and refuses
one chezmoi already manages, pointing at `cheztui edit` (DEC-19): the path
is refused when `chezmoi managed --format=json --path-style=all <path>`
lists anything, which for a directory includes a managed entry under it,
since `chezmoi add` would overwrite that entry's source (DEC-31). A
directory with managed descendants uses the "contains managed entries"
refusal even when chezmoi also lists the directory itself.

`managed` can reject a path outside the destination before the dry run.
For this path rejection only, read the effective destination from
`dump-config`; when the path is outside it, continue to the pre-flight and
add dry run so that the invalid path is a refusal. A failed managed read
for a path inside the destination still exits 3 (§7, DEC-25, DEC-32).

After the pre-flight (§5) it runs the dry run, `chezmoi add --dry-run
--verbose --no-pager --color=false --use-builtin-diff [--template]
[--encrypt] <path>`, whose output is the diff. A failed dry run (a path
that doesn't exist, or isn't under the destination) is a refusal carrying
chezmoi's last error line; empty output (an ignored path) is the refusal
`chezmoi would add nothing to the source`, with chezmoi's last stderr line
when there is one. `--dry-run` (or `-n`) stops there; otherwise `chezmoi
add [--template] [--encrypt] <path>` runs through the write path, and
`verified` is true when `managed` now lists the path and `chezmoi verify
<path>` exits 0. If `managed` doesn't list it, the error is `chezmoi added
nothing`, exit 1.

The diff is masked whole for `--encrypt` and when the path, or any regular
file under a directory path, matches `always_mask`, unless `--reveal`.
Output follows `edit`'s: result lines `added ~/.foo → a1b2c3d "Add .foo"
(pushed)` and `would add ~/.foo (dry run)`. `--json` output: `{target,
source, template, encrypted, diff, commit, pushed, verified}`, where
`template` and `encrypted` are the flags given and `source` is `null` on a
dry run.

### Skill

`skills/cheztui/SKILL.md` ships in the repo and is deployed by tack (as
corral's is). It covers: when to use cheztui (any change to a file that is
or might be chezmoi-managed), use the CLI with `--json` not the TUI, the
edit flow (read source → write spec → `--dry-run` → apply), ops reference,
new files, exit codes, and read commands for looking before acting.

**Retiring `chezmoi-edit`:** once cheztui v0.1 is released and the
`cheztui` skill is deployed, it replaces the `chezmoi-edit` skill.

## 9. Configuration

`~/.config/cheztui/config.toml` (`$XDG_CONFIG_HOME/cheztui/config.toml`
when `XDG_CONFIG_HOME` is set and not empty). A missing file means the
defaults below; cheztui creates the file only when the settings screen
saves. An unknown key or table is kept and ignored. A config error (CLI
exit 2; the TUI shows the error and doesn't start) is any of: a file that
can't be read (a directory at the path, no read permission); a file that
isn't valid TOML; a known table that isn't a table; a value of the wrong
type (a boolean is not an integer, nor an integer a boolean); a negative
`ui.diff_context` or `refresh.debounce_ms`; a `ui.narrow_breakpoint` below
1; a `secrets.mask_patterns` entry that isn't a valid Python regular
expression. `ui.theme` must be a non-empty string; whether Textual knows the
theme is checked when the TUI starts, with the same error. Saving keeps the
file's comments and order (tomlkit) and writes the file in place, so a
symlinked config stays a symlink.

The settings screen (`,`) edits the scalar settings (`ui.*`, `refresh.*`,
`secrets.token_formats`) in place; `e` there opens the file in `$EDITOR` for
the lists. Each table arrives with the feature that reads it: v0.1 reads
`[ui]`, `[refresh]` and `[secrets]`.

The screen (DEC-33) lists one row per scalar, `key  value`, in the order
`ui.theme`, `ui.diff_context`, `ui.narrow_breakpoint`, `refresh.watch`,
`refresh.debounce_ms`, `secrets.token_formats`. `enter` on a boolean
toggles it; on `ui.theme` it opens a list of the themes Textual knows; on
an integer it opens an input on the row. A change is validated as the file
would be, saved at once, and takes effect at once (the theme and the
layout immediately; the others where they are next read). An invalid value
shows its error on the row and isn't saved. `e` suspends the TUI and runs
`$VISUAL`, else `$EDITOR`, else `vi`, split as shell words, on the config
file; afterwards cheztui reloads it, and a config error shows on the screen
while the settings from before the edit stay in effect. `esc` closes the
screen. `-n` doesn't apply here: it covers chezmoi calls, not cheztui's own
config.

```toml
[ui]
theme = "textual-dark"          # any Textual theme
diff_context = 3
narrow_breakpoint = 100

[adopt]
roots = ["~/.config", "~/.local/bin", "~/.ssh"]

[secrets]
mask_patterns = ["token", "secret", "password", "passwd", "api[_-]?key"]
token_formats = true            # ghp_, sk-, AKIA, xox, PEM blocks…
never_mask = ["~/.config/karabiner/**", "~/.config/ghostty/config"]
always_mask = ["~/.netrc", "~/.ssh/id_*", "**/.env"]

[refresh]
watch = true
debounce_ms = 300

[preview]
docker_images = ["ubuntu:24.04", "debian:12"]
ssh_hosts = []

[profiles.work-linux]           # machine override profiles for Data/Preview
os = "linux"
hostname = "work-laptop"
```

## 10. Architecture

```
src/cheztui/
  cli.py          argparse entry; `cheztui` → TUI, subcommands → ops
  ops.py          all behaviour; returns dataclasses; used by CLI and TUI
  runner.py       the async subprocess runner chezmoi.py and git.py share
  chezmoi.py      async wrapper around the chezmoi binary (--no-tty,
                  --config/--source, -n); typed parsers for each JSON output
  git.py          source-repo queries (porcelain status, ahead/behind, log, HEAD)
  edit.py         ops-spec engine + `_editor` entry point (in-place writes)
  secrets.py      masking
  state.py        chezmoi persistent-state reads/resets
  preview.py      temp-dir / docker / ssh back ends
  config.py       TOML load/save
  watch.py        filesystem watching (watchfiles)
  tui/
    app.py        app, header, view tabs, log line, key bar
    views/        drift.py files.py scripts.py data.py externals.py
                  adopt.py secrets.py health.py repo.py preview.py
    widgets/      diff.py (highlighted unified diff), confirm.py (preview
                  modal), picker.py (flags/attributes), tree.py
```

- One chezmoi call per data source per refresh; results cached and shared
  across views.
- Watching (§5, Refresh): the source dir recursively, ignoring `.git/`, and
  the parent directory of each managed target, not recursively, keeping
  only events on managed paths. Events are debounced by
  `refresh.debounce_ms`; events during cheztui's own write are dropped, and
  the write's own refresh covers them. `refresh.watch = false` leaves only
  `g` and the post-write refresh.
- `chezmoi.py` takes a runner, so TUI tests swap in `tests/fake_chezmoi.py`
  and integration tests run the real binary against scratch dirs. A runner
  is an async callable `(argv, *, cwd=None, env=None, stdin=None)` returning
  the exit code, stdout and stderr as bytes; `env` is merged over the
  process environment. The default runner uses
  `asyncio.create_subprocess_exec`, and kills the child when the awaiting
  task is cancelled (DEC-6). `git.py` takes the same runner.
- Interactive writes can take a front-end-supplied runner for terminal I/O;
  `ops.py` still owns the command, pre-flight and result, and `chezmoi.py`
  invokes that runner with the usual full argv. Each command can carry its
  own immutable environment overrides; interactive overrides apply only
  to interactive commands and take precedence over those overrides.
  The TUI supplies suspension,
  not a second write implementation. Write tasks protect the subprocess and
  result collection from the read runner's cancellation behavior (§5).
- Every chezmoi call is `chezmoi --no-tty`, then `--config`, `--source`,
  `--destination` and `--dry-run` when set, then the command. The version
  check (DEC-21) runs `chezmoi --version` once per run, called by the front
  end at start; the reads don't repeat it. `status` runs with
  `--path-style=absolute`.
- Errors: chezmoi missing, too old, or printing output its parser can't read
  is "chezmoi missing or broken" (CLI exit 3); a chezmoi command that exits
  non-zero is a failure carrying its stderr, whose last line is what the log
  line shows (§5).
- `git.py` runs git in chezmoi's `workingTree` (from `dump-config`), with
  `GIT_OPTIONAL_LOCKS=0` so its reads never take the index lock from a
  chezmoi write. The stray-file list is `git status --porcelain=v1 -z
  --untracked-files=all`, new paths for renames.
- Prefer machine-readable output (`--format=json`, `--path-style=all`,
  `--no-pager`); parse text only where chezmoi has no JSON (`status`,
  `doctor`), with parsers that handle paths containing spaces.

## 11. Testing

- **Never touch the real source dir or home.** Tests build a scratch HOME
  and source dir (a real git repo) per test and point chezmoi at them with
  `--source`, `--destination`, `--config`, with `HOME` and
  `XDG_CONFIG_HOME` set to scratch dirs too. One shared pytest fixture
  builds this setup; the scratch config never enables `autoPush`, and the
  scratch repo has no remote (a test of push state sets a local branch as
  the upstream). The fixture also sets `XDG_DATA_HOME`, `XDG_CACHE_HOME`
  and `XDG_STATE_HOME` to scratch dirs, `GIT_CONFIG_NOSYSTEM=1`, and
  `GIT_CONFIG_GLOBAL` to a scratch gitconfig with a test identity. The
  chezmoi config sits in its own scratch dir, never the scratch home or a
  dir containing it: chezmoi refuses to add anything under its config
  file's dir.
- **Recorded fixtures** (`tests/fixtures/chezmoi/`) come from real chezmoi
  in a scratch setup, with the scratch root replaced by `/scratch` so no
  machine path is committed; a README there names the chezmoi version and
  the commands.
- **Unit:** parsers against recorded chezmoi output fixtures; ops engine
  (every op, count assertions, skip, failure leaves file untouched);
  masking.
- **Integration:** `ops.py` against a real `chezmoi` binary in the scratch
  setup (skipped if chezmoi isn't installed).
- **Fake:** `tests/fake_chezmoi.py` for TUI tests that need canned states
  (drift in both directions, scripts, externals, errors). It extends the
  write path's fake (`tests/fake_write.py`), so one fake answers chezmoi and
  git, and its drifted state replays the recorded fixtures, so it can't
  drift from what real chezmoi prints.
- **TUI:** `App.run_test()` + snapshot tests (pytest-textual-snapshot) for
  each view and the preview modal.
- CI: macOS and Linux, pytest, ruff check/format, ty, packaging check. The
  test job installs chezmoi at the floor version (DEC-21), so integration
  tests run in CI against the oldest chezmoi cheztui supports, while a
  maintainer's machine runs them against a current one (DEC-24).

## 12. Milestones

Each milestone is releasable.

**v0.1 — Drift and edit** (enough to retire `chezmoi-edit`)
- App frame: header, view tabs, list/detail, log line, key bar, help,
  palette, settings.
- Drift view with direction, built-in diff, apply / re-add / merge / edit /
  full diff, multi-select, apply-all.
- Write flow (pre-flight, preview modal, result with commit).
- Masking for diffs and rendered output, with `R` reveal (DEC-17).
- Watch + manual refresh; `--dry-run`.
- CLI: `status`, `edit`, `add`, `verify`, all with `--json`.
- `skills/cheztui/SKILL.md`; plugin manifest; PyPI + Homebrew.

**v0.2 — Files and Adopt**
- Files view (tree, attributes, chattr picker, forget/destroy, special
  files group).
- Adopt view (unmanaged, ignored with matched rule, add flags, ignore,
  `.chezmoiremove`).
- CLI: `files`, `unmanaged`.

**v0.3 — Scripts and Data**
- Script ledger with state reset.
- Data & Templates workbench, live render, machine override profiles.
- CLI: `scripts`, `scripts reset`, `data`, `render`.

**v0.4 — Secrets, Externals, Health**
- Secrets & Encryption view (masking itself ships in v0.1).
- Externals view with refresh.
- Health view (doctor, verify, upgrade).
- CLI: `externals`, `doctor`.

**v0.5 — Repo and Preview**
- Repo view (log, pull+apply with preview, push, restore, lazygit).
- Preview: temp dir, then Docker and SSH.
- CLI: `preview`.

## 13. Settled decisions

These questions were open in the first draft and are now settled.

- **Restore from a commit** stays in the Repo view. It writes through
  `chezmoi edit --apply` like every other write (§6, Repo).
- **Masking** uses name patterns plus token-format detection, with
  `never_mask` and `always_mask` globs in config (§5, §9).
- **`cheztui edit`** takes one target per call (§8).
- **The Homebrew formula** declares `depends_on "chezmoi"` (§2).
