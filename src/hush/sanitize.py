import json
import re
from collections.abc import Callable, Mapping

# CSI sequences, OSC sequences (terminated by BEL or ST), and two-byte escapes.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Control characters except tab and newline (C0, DEL, C1), and invisible formatting characters
# that people cannot see but a model can read: soft hyphen, zero-width, bidi controls, BOM,
# and the Unicode "tag" block used for hidden text.
_CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\xad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000e0000-\U000e007f]"
)

MAX_LINE_CHARS = 4000

# Restrict key bodies to PEM characters so that an unterminated header does not hide
# unrelated output. Multiline bodies require full PEM lines, preserving lines like `app.py:1`.
# The other form handles a block printed on one line with escaped newlines.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:[A-Za-z0-9+/=:,\\ \t-]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:\n(?!-----END )[A-Za-z0-9+/=:, \t-]*(?=\n|\Z))*(?:\n-----END [A-Z ]*PRIVATE KEY-----)?)"
)
_SECRET_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[A-Za-z0-9_-]{30,}",
        r"\bnpm_[A-Za-z0-9]{30,}",
        r"\bxox[abeprs]-[A-Za-z0-9-]{10,}",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"(?i)\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]{8,}",
        # user:password@ inside a URL.
        r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)",
    )
)
# Environment variable names that hold secrets. PWD and OLDPWD are directories, not passwords.
_SECRET_ENV_NAME = re.compile(
    r"(?i)token|secret|passw|credential|api[_-]?key|private[_-]?key|(?:^|_)(?:key|pass|pwd|auth|dsn)(?:$|_)"
)
_NOT_SECRET_ENV_NAMES = frozenset({"PWD", "OLDPWD"})
_MIN_SECRET_LENGTH = 8
REDACTED = "[REDACTED]"


def is_secret_env_name(name: str) -> bool:
    return name not in _NOT_SECRET_ENV_NAMES and bool(_SECRET_ENV_NAME.search(name))


def _clean_line(line: str, redact: Callable[[str], str] | None) -> str:
    """One line with escape codes gone, `\\r` overwrites resolved like a terminal, and clipped."""
    line = _CONTROL.sub("", _ANSI.sub("", line.removesuffix("\r").rsplit("\r", 1)[-1]))
    if len(line) > MAX_LINE_CHARS:
        # A secret cut by the clip would leave its first part unmatched, so redact before it.
        if redact:
            line = redact(line)
        if len(line) > MAX_LINE_CHARS:
            line = f"{line[:MAX_LINE_CHARS]}…[{len(line) - MAX_LINE_CHARS} more characters]"
    return line


def normalize(text: str, redact: Callable[[str], str] | None = None) -> str:
    """Remove terminal controls and invisible characters, then clip long lines.

    Apply `redact` only to lines that exceed the line limit. The caller redacts the remaining
    lines after parsing so that file names remain available for lookups.
    """
    return "\n".join(_clean_line(line, redact) for line in text.split("\n"))


class Redactor:
    """Mask recognised token formats and values from secret environment variables."""

    def __init__(self, *environs: Mapping[str, str]) -> None:
        """`environs` are separate mappings, so a name in two of them has both of its values masked."""
        values: set[str] = set()
        for name, value in (pair for environ in environs for pair in environ.items()):
            if not is_secret_env_name(name):
                continue
            if len(value) >= _MIN_SECRET_LENGTH:
                # Also the forms a value takes inside JSON and repr output.
                values.update({value, json.dumps(value)[1:-1], repr(value)[1:-1]})
        self._env_values = tuple(sorted(values, key=len, reverse=True))
        self._multiline_values = tuple(v for v in self._env_values if "\n" in v)

    def redact_multiline(self, text: str) -> str:
        """Mask only what spans lines. Run it on the whole text, before it is split into lines."""
        for value in self._multiline_values:
            text = text.replace(value, REDACTED)
        return _PRIVATE_KEY.sub(REDACTED, text)

    def redact(self, text: str) -> str:
        text = self.redact_multiline(text)
        for value in self._env_values:
            text = text.replace(value, REDACTED)
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text
