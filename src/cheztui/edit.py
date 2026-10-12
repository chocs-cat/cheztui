"""Pure ops-spec engine and hash-checked editor (spec §8; DEC-11, DEC-18, DEC-31)."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SpecError(ValueError):
    """An edit refused before writing anything."""


@dataclass(frozen=True)
class Spec:
    raw: Any
    ops: list[Any]
    skip_if_contains: str | None = None


def spec_from_json(value: Any) -> Spec:
    obj = {"ops": value} if isinstance(value, list) else value
    if not isinstance(obj, dict):
        raise SpecError("edit spec must be a JSON object or list of ops")
    ops = obj.get("ops")
    if not isinstance(ops, list) or not ops:
        raise SpecError("edit spec has no 'ops' list")
    skip = obj.get("skip_if_contains")
    if skip is not None and not isinstance(skip, str):
        raise SpecError("'skip_if_contains' must be a string")
    return Spec(value, ops, skip)


def parse_spec(text: str) -> Spec:
    try:
        value = json.loads(text)
    except ValueError as e:
        raise SpecError(f"edit spec is not valid JSON: {e}") from None
    return spec_from_json(value)


def _count(label: str, found: int, want: Any) -> int:
    if want == "all":
        if not found:
            raise SpecError(f"{label}: no match found")
        return found
    if type(want) is not int or want < 1:
        raise SpecError(f"{label}: 'count' must be a positive integer or \"all\"")
    if not found:
        raise SpecError(f"{label}: no match found (expected {want})")
    if found != want:
        raise SpecError(
            f"{label}: matched {found} time(s) but expected {want} - make the match string "
            'more specific, or set "count"'
        )
    return found


def _fields(op: dict[str, Any], label: str, *names: str) -> list[str]:
    if any(op.get(n) is None for n in names):
        needed = " and ".join(repr(n) for n in names)
        raise SpecError(f"{label}: needs {needed}")
    for name in names:
        if not isinstance(op[name], str):
            raise SpecError(f"{label}: '{name}' must be a string")
    return [op[n] for n in names]


def _apply_op(text: str, op: Any, index: int) -> str:
    if not isinstance(op, dict):
        raise SpecError(f"op {index} is not an object")
    kind = op.get("op")
    label = f"op {index} ({kind})"
    count = op.get("count", 1)
    if kind in ("replace", "delete", "insert_after", "insert_before"):
        if kind == "replace":
            old, new = _fields(op, label, "old", "new")
        elif kind == "delete":
            (old,) = _fields(op, label, "old")
            new = ""
        else:
            old, ins = _fields(op, label, "anchor", "text")
            new = old + ins if kind == "insert_after" else ins + old
        n = _count(label, text.count(old), count)
        return text.replace(old, new, n if count != "all" else -1)
    if kind in ("append", "prepend"):
        (ins,) = _fields(op, label, "text")
        if kind == "prepend":
            return ins + text
        return text + ("\n" if text and not text.endswith("\n") else "") + ins
    if kind == "regex_replace":
        pattern, repl = _fields(op, label, "pattern", "repl")
        flag_text = op.get("flags", "")
        if not isinstance(flag_text, str):
            raise SpecError(f"{label}: 'flags' must be a string")
        flags = 0
        for ch in flag_text:
            flag = {"m": re.M, "s": re.S, "i": re.I, "x": re.X}.get(ch)
            if flag is None:
                raise SpecError(f"{label}: unknown regex flag {ch!r} (use m, s, i, x)")
            flags |= flag
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise SpecError(f"{label}: bad regex: {e}") from None
        n = _count(label, len(rx.findall(text)), count)
        try:
            return rx.sub(repl, text, count=0 if count == "all" else n)
        except (re.error, IndexError) as e:
            raise SpecError(f"{label}: bad replacement: {e}") from None
    raise SpecError(f"{label}: unknown op {kind!r}")


def apply_spec(spec: Spec, text: str, name: str) -> str | None:
    if spec.skip_if_contains and spec.skip_if_contains in text:
        return None
    result = text
    for index, op in enumerate(spec.ops, 1):
        result = _apply_op(result, op, index)
    if result == text:
        raise SpecError(f"ops produced no change to {name} - the spec is probably wrong")
    return result


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unified_diff(old: str, new: str, name: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        n=3,
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def editor_main(argv: list[str]) -> int:
    try:
        if not argv:
            raise SpecError("no file argument; this script is meant to be run as $EDITOR")
        if len(argv) != 1:
            raise SpecError(
                f"got {len(argv)} files ({', '.join(argv)}) but an edit spec targets one file - "
                "run `chezmoi edit` on a single target at a time"
            )
        spec_path = os.environ.get("CHEZTUI_EDIT_SPEC")
        if not spec_path:
            raise SpecError("set CHEZTUI_EDIT_SPEC to a JSON edit spec")
        try:
            payload = json.loads(Path(spec_path).read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as e:
            raise SpecError(f"cannot read CHEZTUI_EDIT_SPEC {spec_path}: {e}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("sha256"), str):
            raise SpecError("CHEZTUI_EDIT_SPEC needs 'spec' and 'sha256'")
        spec = spec_from_json(payload.get("spec"))
        path = Path(argv[0])
        try:
            original = path.read_bytes().decode("utf-8")
        except UnicodeError:
            raise SpecError(f"{path} isn't UTF-8 text") from None
        result = apply_spec(spec, original, str(path))
        if result is None or sha256(result) != payload["sha256"]:
            raise SpecError("edit result doesn't match the expected sha256")
        # In place, preserving the inode and exact line endings (DEC-18, DEC-31).
        with path.open("wb") as f:
            f.write(result.encode("utf-8"))
    except (SpecError, OSError) as e:
        print(f"cheztui _editor: {e}", file=sys.stderr)
        return 1
    return 0
