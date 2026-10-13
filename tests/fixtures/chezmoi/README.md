# Recorded chezmoi output

The parser tests in `tests/test_chezmoi_reads.py` read these files. They were
recorded with **chezmoi v2.60.0**, the floor cheztui supports (DEC-21), from
one scratch setup that `build_drift` in that test file builds (spec §11). The
scratch root is replaced with `/scratch`, so no machine path is committed;
`/opt/ejson/keys` in `dump-config.json` is chezmoi's own default.

| File | Command |
|---|---|
| `status.txt` | `chezmoi status --path-style=absolute` |
| `managed.json` | `chezmoi managed --format=json --path-style=all` |
| `managed-no-scripts.json` | `chezmoi managed --format=json --path-style=all --exclude=scripts` |
| `managed-templates.json` | `chezmoi managed --format=json --path-style=all --include=templates` |
| `dump.json` | `chezmoi dump --format=json` |
| `state-dump.json` | `chezmoi state dump --format=json` |
| `dump-config.json` | `chezmoi dump-config --format=json` |
| `diff.json` | `chezmoi diff --no-pager --color=false --use-builtin-diff <target>` for each status row, keyed by the target |
| `preview.json` | the write previews' dry runs (`ops.PREVIEW_FLAGS`: `--dry-run --verbose --no-pager --color=false --use-builtin-diff`): `apply --force --recursive=false <target>` for each status row, and `re-add --recursive=false <target>` for each `you→` row whose target is a file, keyed by the action, then the target. They run before anything writes |

Every command also runs with `--no-tty` and scratch `--config`, `--source`
and `--destination`.

The setup has a `you→` row (`.mine`, `MM`), a `both` row whose name has a
space (`.both file`, `MM`), a `src→` row on an executable file (`.tool`,
` M`), a `src→` row on a template whose source changed after the apply
(`.tmplfile`, ` M`), an onchange script (`hello.sh`, ` R`), a deleted
target (`.gone`, `DA`), a directory (`.dir`) and a symlink (`.link`).

To record them again, with the chezmoi you want first on `PATH`:

```sh
CHEZTUI_RECORD_FIXTURES=1 uv run pytest tests/test_chezmoi_reads.py -k record
```

Update the version above when you do.
