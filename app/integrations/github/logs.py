"""Actions log handling (docs/20-github-actions-webhooks.md): the log-host restriction, the
in-memory unzip with caps, ANSI stripping, secret redaction and the tail-first output cap.

Order matters: ANSI codes are stripped first (an escape sequence can split a token), the
text is cut tail-first at a line boundary (so no token is split by the cut), then every
secret-shaped string is redacted, then the result is capped once more.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from app.integrations.github.redact import REDACTED, redact

# --- the log host restriction -------------------------------------------------------------


def log_host_allowed(url: str, patterns: Sequence[str]) -> bool:
    """True if `url` is https, has no credentials or odd port, and its host matches one of
    `patterns` label by label (`*` never spans a dot, so `*.x.com` matches `a.x.com` only)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    labels = host.split(".")
    for pat in patterns:
        plabels = pat.strip().lower().split(".")
        if len(plabels) == len(labels) and all(
            fnmatchcase(h, p) for h, p in zip(labels, plabels, strict=True)
        ):
            return True
    return False


# --- unzip --------------------------------------------------------------------------------


class LogArchiveError(ValueError):
    """The run-log archive is not a zip, or breaks a size / entry cap."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def unzip_logs(data: bytes, *, max_total: int, max_entries: int) -> tuple[str, bool]:
    """The run-log zip -> (one text, capped). GitHub's archive holds one `N_job.txt` per job
    at the top level plus the same logs split per step in folders: the top-level files are
    used when there are any, else every .txt. Each entry is read with a bounded read, so a
    lying header cannot inflate past `max_total`."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise LogArchiveError("log_archive_invalid", "the run-log archive is not a zip") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > max_entries:
            raise LogArchiveError(
                "log_archive_too_many_entries", f"the run-log archive has over {max_entries} files"
            )
        texts = [i for i in infos if i.filename.endswith(".txt") and ".." not in i.filename]
        top = [i for i in texts if "/" not in i.filename.strip("/")]
        chosen = sorted(top or texts, key=lambda i: _entry_order(i.filename))
        parts: list[str] = []
        total = 0
        capped = False
        for info in chosen:
            budget = max_total - total
            if budget <= 0:
                capped = True
                break
            try:
                with zf.open(info) as fh:
                    raw = fh.read(budget + 1)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, OSError):
                raise LogArchiveError(
                    "log_archive_invalid", "a run-log archive entry cannot be read"
                ) from None
            if len(raw) > budget:
                raw, capped = raw[:budget], True
            total += len(raw)
            parts.append(f"===== {info.filename} =====\n" + raw.decode("utf-8", "replace"))
    return "\n".join(parts), capped


def _entry_order(name: str) -> tuple[int, str]:
    head = name.rsplit("/", 1)[-1].split("_", 1)[0]
    return (int(head) if head.isdigit() else 1_000_000, name)


# --- sanitising ---------------------------------------------------------------------------

# CSI (colours, cursor), OSC (titles, hyperlinks), and the remaining 2-byte escapes.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[@-Z\\-_]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
# Other C0 controls except \t and \n (\r is folded first).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", _ANSI.sub("", text))


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
)
_ORPHAN_KEY_END = re.compile(r"\A[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")
_SIMPLE: tuple[re.Pattern[str], ...] = (
    # AWS access key ids (AKIA long-term, ASIA temporary, and the other documented prefixes).
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA|APKA)[A-Z0-9]{16}\b"),
    # JWTs.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Slack, Stripe, Google API keys, npm and PyPI tokens.
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{32,}"),
)
# Header-style credentials: Authorization (any scheme), X-Api-Key and friends.
_HEADER = re.compile(
    r"(?im)\b((?:proxy-)?authorization\s*[:=]\s*[\"']?)(?:(?:bearer|token|basic|digest)\s+)?"
    r"[^\s\"',;]+"
    r"|\b((?:x-)?api[-_]?key\s*[:=]\s*[\"']?)[^\s\"',;]+"
)
_URL_CREDS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^:/@\s]+:[^@/\s]+@")
# NAME=value / NAME: value. Redacted when the name says secret, or the value looks random.
_ASSIGN = re.compile(
    r"(?P<name>\b[A-Za-z_][A-Za-z0-9_.-]{0,80})(?P<sep>\s*[=:]\s*)(?P<q>[\"']?)"
    r"(?P<value>[^\s\"']{6,})"
)
_SECRET_NAME = re.compile(
    r"(?i)(?:secret|passw(?:or)?d|passwd|pwd|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|auth$|authorization|signature|session[_-]?key|client[_-]?secret|_key$|^key$)"
)
# Names that hold a value which looks random but is not a secret.
_SAFE_NAME = re.compile(r"(?i)(?:sha|hash|digest|commit|run_id|job_id|uuid|id)$")
HIGH_ENTROPY_BITS = 4.0
HIGH_ENTROPY_MIN_LEN = 20


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in Counter(value).values())


def _high_entropy(value: str) -> bool:
    if len(value) < HIGH_ENTROPY_MIN_LEN:
        return False
    if value.startswith(("/", "http", "./", "~")) or value.count("/") >= 2:
        return False  # URLs and paths
    classes = sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
        )
    )
    return classes >= 2 and shannon_entropy(value) > HIGH_ENTROPY_BITS


def _assign(m: re.Match[str]) -> str:
    name, value = m["name"], m["value"]
    if value == REDACTED or value.startswith("***"):
        return m.group(0)
    secret_name = bool(_SECRET_NAME.search(name)) and not _SAFE_NAME.search(name)
    if secret_name or (_high_entropy(value) and not _SAFE_NAME.search(name)):
        return f"{name}{m['sep']}{m['q']}{REDACTED}"
    return m.group(0)


def redact_log(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Redact credentials in a log: known token shapes (GitHub, AWS, JWT, Slack, ...),
    Authorization / API-key headers, URL credentials, private-key blocks, and `NAME=value`
    pairs whose name says secret or whose value is high-entropy."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    if "PRIVATE KEY-----" in text and "-----BEGIN" not in text:
        # The tail cut removed the BEGIN line: everything up to END is key material.
        text = _ORPHAN_KEY_END.sub(REDACTED, text, count=1)
    text = redact(text, secrets)
    for pat in _SIMPLE:
        text = pat.sub(REDACTED, text)
    text = _HEADER.sub(lambda m: (m.group(1) or m.group(2) or "") + REDACTED, text)
    text = _URL_CREDS.sub(lambda m: m.group(1) + REDACTED + "@", text)
    return _ASSIGN.sub(_assign, text)


def tail(text: str, max_bytes: int) -> tuple[str, bool]:
    """The last `max_bytes` (UTF-8) of `text`, cut at a line start. -> (text, truncated)."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[-max_bytes:]
    nl = cut.find(b"\n")
    if 0 <= nl < len(cut) - 1:
        cut = cut[nl + 1 :]
    return cut.decode("utf-8", "ignore"), True


@dataclass(frozen=True, slots=True)
class SanitizedLog:
    text: str
    truncated: bool
    original_bytes: int


def sanitize(
    text: str, *, max_bytes: int, secrets: Iterable[str | None] = (), truncated: bool = False
) -> SanitizedLog:
    """strip ANSI -> tail cut -> redact -> final cap, with a marker line when cut."""
    original = len(text.encode("utf-8"))
    clean = strip_ansi(text)
    clean, cut1 = tail(clean, max_bytes)
    clean = redact_log(clean, secrets)
    marker_room = 128
    clean, cut2 = tail(clean, max_bytes - marker_room)
    cut = truncated or cut1 or cut2
    if cut:
        clean = f"[log truncated: showing the last {len(clean.encode())} bytes]\n" + clean
    return SanitizedLog(clean, cut, original)


__all__ = [
    "LogArchiveError",
    "SanitizedLog",
    "log_host_allowed",
    "redact_log",
    "sanitize",
    "shannon_entropy",
    "strip_ansi",
    "tail",
    "unzip_logs",
]
