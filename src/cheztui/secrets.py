"""Pure masking of one target's body or diff (spec §5 Secrets)."""

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from cheztui.config import SecretsConfig
from cheztui.edit import split_lines

MASK = "********"

_FAMILIES = (
    "onepassword|bitwarden|lastpass|keepassxc|keeper|dashlane|doppler|gopass|rbw|"
    "protonPass|awsSecretsManager|hcpVaultSecret|ejsonDecrypt"
)
_FUNCTIONS = (
    rf"(?:(?:{_FAMILIES})[A-Za-z]*|azureKeyVault|pass|passFields|passRaw|passhole|"
    r"secret|secretJSON|keyring|vault|decrypt)"
)
_SECRET_CALL = re.compile(rf"(?:\|\s*\b{_FUNCTIONS}\b|\b{_FUNCTIONS}\b\s+[\"`$(.])")
_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_KEY_VALUE = re.compile(
    r"^[ \t]*(?:export[ \t]+|-[ \t]+)?(?P<quote>['\"]?)(?P<key>[A-Za-z0-9_.-]+)"
    r"(?P=quote)[ \t]*[=:][ \t]*"
)
_TOKENS = tuple(
    re.compile(r"(?<![A-Za-z0-9_])" + pattern)
    for pattern in (
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])",
        r"xox[abposr]-[A-Za-z0-9-]{10,}",
    )
)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


@dataclass(frozen=True)
class Masked:
    text: str
    masked: bool


def _segment_regex(segment: str) -> str:
    result = ""
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            result += "[^/]*"
        elif char == "?":
            result += "[^/]"
        elif char == "[":
            end = index + 1
            if end < len(segment) and segment[end] == "!":
                end += 1
            if end < len(segment) and segment[end] == "]":
                end += 1
            end = segment.find("]", end)
            if end == -1:
                result += r"\["
            else:
                # Only the character class uses fnmatch; stars never cross '/'.
                translated = fnmatch.translate(segment[index : end + 1])
                result += "(?!/)" + translated[4:-3]
                index = end
        else:
            result += re.escape(char)
        index += 1
    return result


def _glob_regex(pattern: str, home: Path) -> re.Pattern[str]:
    # '~' is chezmoi's destination, not the process home (DEC-26).
    prefix = ""
    if pattern.startswith("~/"):
        prefix = re.escape(str(home).rstrip("/"))
        pattern = "/" + pattern[2:]
    elif not pattern.startswith("/"):
        prefix = re.escape(str(home).rstrip("/"))
        pattern = "/" + pattern
    parts: list[str] = []
    for part in pattern.split("/"):
        if part != "**" or not parts or parts[-1] != "**":
            parts.append(part)
    result = ""
    for index, part in enumerate(parts):
        if part == "**":
            if index == len(parts) - 1:
                result += "(?:/[^/]+)*"
            else:
                result += "/(?:[^/]+/)*"
        else:
            if index and parts[index - 1] != "**":
                result += "/"
            result += _segment_regex(part)
    return re.compile(prefix + result)


def glob_matches(pattern: str, path: Path, home: Path) -> bool:
    """Match a destination-relative glob against an absolute target path."""
    return _glob_regex(pattern, home).fullmatch(str(path)) is not None


def calls_secret_function(source: str) -> bool:
    """Recognize the password-manager call forms in spec §5 Secrets."""
    return _SECRET_CALL.search(source) is not None


def _split_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line and line[-1] in _ENDINGS:
        return line[:-1], line[-1]
    return line, ""


class Masker:
    def __init__(self, config: SecretsConfig, home: Path):
        self.home = home
        self._patterns = tuple(re.compile(p, re.IGNORECASE) for p in config.mask_patterns)
        self._always = tuple(_glob_regex(p, home) for p in config.always_mask)
        self._never = tuple(_glob_regex(p, home) for p in config.never_mask)
        self._token_formats = config.token_formats

    def masks_whole(
        self, target: Path, *, encrypted: bool = False, secret_template: bool = False
    ) -> bool:
        return (
            encrypted
            or secret_template
            or any(p.fullmatch(str(target)) is not None for p in self._always)
        )

    def _name_span(self, content: str) -> tuple[int, int] | None:
        match = _KEY_VALUE.match(content)
        if match is None or not any(p.search(match["key"]) for p in self._patterns):
            return None
        start = match.end()
        if start == len(content):
            return None
        if content[start] in "\"'":
            quote = content[start]
            start += 1
            end = start
            while end < len(content) and content[end] != quote:
                # Double-quoted escapes consume pairs; only odd backslash runs escape a quote.
                end += 2 if quote == '"' and content[end] == "\\" else 1
            end = min(end, len(content))
        else:
            end = len(content.rstrip())
            if content[end - 1 : end] == ",":
                end = len(content[: end - 1].rstrip())
            if content[start:end] in ("{", "["):
                return None
        return (start, end) if start < end else None

    def _mask_line(
        self, content: str, *, whole: bool, line_level: bool, pem: bool
    ) -> tuple[str, bool, bool]:
        """Mask content without a line ending or diff prefix; carry PEM state."""
        if whole:
            return (MASK if content else "", pem, bool(content))
        if not line_level:
            return content, pem, False
        if self._token_formats:
            if _PEM_END.search(content):
                return content, False, False
            if _PEM_BEGIN.search(content):
                return content, True, False
            if pem:
                return MASK, True, True
        spans: list[tuple[int, int]] = []
        name_span = self._name_span(content)
        if name_span is not None:
            spans.append(name_span)
        if self._token_formats:
            spans.extend(match.span() for pattern in _TOKENS for match in pattern.finditer(content))
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        output: list[str] = []
        cursor = 0
        for start, end in merged:
            output.extend((content[cursor:start], MASK))
            cursor = end
        output.append(content[cursor:])
        return "".join(output), pem, bool(merged)

    def _mask_diff(self, text: str, *, whole: bool, line_level: bool) -> Masked:
        output: list[str] = []
        old = new = 0
        pem = masked = False
        # At `\n` alone, as git ends a diff line, or a lone `\r` would end the
        # hunk early and pass the rest through unmasked (spec §5 Diffs, DEC-40).
        for line in split_lines(text):
            content, ending = _split_ending(line)
            hunk = _HUNK.match(content)
            if hunk:
                old, new = (int(count) if count is not None else 1 for count in hunk.groups())
            elif old > 0 or new > 0:
                # Empty lines are empty context, including while a PEM block is open.
                if not content:
                    old -= 1
                    new -= 1
                elif content[0] in " +-":
                    prefix, body = content[0], content[1:]
                    old -= prefix in " -"
                    new -= prefix in " +"
                    if body or prefix != " ":
                        body, pem, changed = self._mask_line(
                            body, whole=whole, line_level=line_level, pem=pem
                        )
                        masked |= changed
                    content = prefix + body
            elif content.startswith("diff "):
                pem = False
            output.append(content + ending)
        return Masked("".join(output), masked)

    def mask(
        self,
        text: str,
        target: Path,
        *,
        diff: bool = False,
        encrypted: bool = False,
        secret_template: bool = False,
        line_level: bool = True,
        whole: bool = False,
        never_mask: bool = True,
    ) -> Masked:
        """Mask one target's body or diff.

        `whole` forces whole masking for text a caller already classified,
        and `never_mask=False` drops that glob's exemption: write diagnostics
        that can't be assigned to one target use both (spec §5 Diagnostics,
        DEC-30).
        """
        whole = whole or self.masks_whole(
            target, encrypted=encrypted, secret_template=secret_template
        )
        line_level = line_level and not (
            never_mask and any(p.fullmatch(str(target)) for p in self._never)
        )
        if not whole and not line_level:
            return Masked(text, False)
        if diff:
            return self._mask_diff(text, whole=whole, line_level=line_level)
        output: list[str] = []
        masked = False
        pem = False
        for line in text.splitlines(keepends=True):
            content, ending = _split_ending(line)
            content, pem, changed = self._mask_line(
                content, whole=whole, line_level=line_level, pem=pem
            )
            masked |= changed
            output.append(content + ending)
        return Masked("".join(output), masked)
