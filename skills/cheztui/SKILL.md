---
name: cheztui
description: Edit files that chezmoi manages with the `cheztui` CLI — scripted, assertion-checked edits through `cheztui edit` and new files through `cheztui add`, each previewed with `--dry-run`, applied, committed and verified in one command. Will replace the chezmoi-edit skill once cheztui v0.1 is released. The edit, add, status and verify commands exist; the full workflow guide arrives with cheztui v0.1.
---

# cheztui

`cheztui edit <target> --ops FILE|- --dry-run` previews a scripted edit;
omit `--dry-run` to apply and verify, and use `--json` for structured output.
`cheztui add <path> --dry-run` previews adding a new file or directory; omit
`--dry-run` to add and verify. Add accepts `--template`, `--encrypt` and `--json`.
`cheztui status --json` and `cheztui verify --json` provide the read commands.
The full guide arrives with cheztui v0.1: when to use cheztui, the
`--json` CLI, the edit flow (read the source, write an ops spec, `--dry-run`,
apply), the ops reference, new files, exit codes, and the read commands to
run before acting. See `docs/spec.md` §8.

Until then, use the `chezmoi-edit` skill.
