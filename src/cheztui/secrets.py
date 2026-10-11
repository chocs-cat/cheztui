"""Pure masking of one target's body or diff (spec §5 Secrets)."""

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from cheztui.config import SecretsConfig

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

    def mask(
        self,
        text: str,
        target: Path,
        *,
        diff: bool = False,
        encrypted: bool = False,
        secret_template: bool = False,
        line_level: bool = True,
    ) -> Masked:
        whole = self.masks_whole(target, encrypted=encrypted, secret_template=secret_template)
        if not whole:
            return Masked(text, False)
        output: list[str] = []
        masked = False
        for line in text.splitlines(keepends=True):
            content, ending = _split_ending(line)
            if content:
                content = MASK
                masked = True
            output.append(content + ending)
        return Masked("".join(output), masked)
