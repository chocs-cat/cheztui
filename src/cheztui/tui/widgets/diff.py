"""A unified diff coloured by line kind (spec §6 Drift, *Body: the diff*; DEC-34).

Not by language: `+` lines `$success`, `-` lines `$error`, `@@` lines
`$accent`, the other header lines bold, context plain. Hunk lines are told
from header lines by the counts in each `@@` line, as masking reads them
(spec §5 Secrets), so an added `++x` line isn't a `+++` header. The Drift
detail shows it; the write preview reuses it.
"""

from __future__ import annotations

import re

from textual.content import Content

_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_HEADERS = (
    "diff ",
    "index ",
    "--- ",
    "+++ ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
)
_KINDS = {"+": "$success", "-": "$error"}


def diff_content(text: str) -> Content:
    """`text`, a unified diff, as coloured content; a final newline is dropped."""
    parts: list[Content] = []
    old = new = 0
    for line in text.removesuffix("\n").split("\n"):
        hunk = _HUNK.match(line)
        if hunk:
            old, new = (int(n) if n is not None else 1 for n in hunk.groups())
            parts.append(Content.styled(line, "$accent"))
        elif (old > 0 or new > 0) and line[:1] in ("", " ", "+", "-"):
            kind = line[:1] or " "
            old -= kind in " -"
            new -= kind in " +"
            parts.append(Content.styled(line, _KINDS[kind]) if kind in _KINDS else Content(line))
        elif line.startswith(_HEADERS):
            parts.append(Content.styled(line, "bold"))
        else:
            parts.append(Content(line))
    return Content("\n").join(parts)
