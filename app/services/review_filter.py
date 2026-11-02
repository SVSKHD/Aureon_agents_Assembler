"""Data classification for the Claude review (docs/22-claude-review.md).

Runs on the unified diff and the PR title and body before anything leaves the server:

- **BLOCKED** (nothing is sent): a touched path is sensitive (`app/core/sensitive_paths.py`:
  secrets, infra, trading/risk, auth), or the text contains something secret-shaped: a
  private key, a GitHub token, an AWS key, a `password=` literal, a secret-named `.env`
  line with a value, or a high-entropy literal assigned to a key/secret/token name.
- **REDACT** (sent with masking): emails, IPv4 addresses and internal hostnames
  (`*.spasta.online` by default, `*.internal`, `*.local`, `*.lan`) are replaced in place.
  Masking is per match, inside a line, so diff line numbers do not move.
- **ALLOW**: nothing found.

Reasons never quote the matched value: only the rule, and the file (and line) it was in.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.sensitive_paths import classify_path

Outcome = Literal["ALLOW", "REDACT", "BLOCKED"]
MAX_REASONS = 20

# --- secret-shaped content (BLOCKED) ----------------------------------------------------------

_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)aws_?secret_?access_?key\s*[\"']?\s*[:=]\s*\S")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("anthropic_or_openai_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@\"']+:[^\s/@\"']+@")),
    # password = "literal" anywhere; password=value as a config line or a URL query
    # parameter; "password": "literal" in JSON/YAML/dicts. `password == x`,
    # `password: str` and `password=PASSWORD` inside a call are not literals.
    ("password_literal", re.compile(r"(?i)pass(?:word|wd)\w*\s*=(?!=)\s*[\"'][^\"'\s]+[\"']")),
    (
        "password_literal",
        re.compile(
            r"(?im)^[+\- ]?[ \t]*(?:export[ \t]+)?pass(?:word|wd)\w*[ \t]*=(?!=)[ \t]*[^\s#]"
        ),
    ),
    ("password_literal", re.compile(r"(?i)[?&;]pass(?:word|wd)=[^&\s]+")),
    ("password_literal", re.compile(r"(?i)pass(?:word|wd)\w*[\"']\s*:\s*[\"'][^\"'\s]+[\"']")),
)
# A `.env`-style line whose name says secret and whose value is non-empty and not a reference.
_ENV_LINE = re.compile(
    r"(?m)^[+\- ]?[ \t]*(?:export[ \t]+)?"
    r"(?P<name>[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|"
    r"ACCESS_KEY|CREDENTIALS?|DSN|DATABASE_URL|REDIS_URL)[A-Z0-9_]*)"
    r"[ \t]*=[ \t]*(?P<value>[^\s#]+)"
)
# key/secret/token-named assignment of a quoted literal: checked for entropy.
_NAMED_LITERAL = re.compile(
    r"(?i)\b[\w.-]*(?:key|secret|token|passw(?:or)?d|credential|auth|salt|signature)[\w.-]*"
    r"[\"']?\s*[:=]\s*[\"'](?P<value>[A-Za-z0-9+/=_\-.~]{16,})[\"']"
)
# any long quoted base64-ish literal: blocked only at a high entropy
_ANY_LITERAL = re.compile(r"[\"'](?P<value>[A-Za-z0-9+/=_\-]{32,})[\"']")
NAMED_ENTROPY = 3.5
ANY_ENTROPY = 4.3

# --- masked content (REDACT) ------------------------------------------------------------------

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")
_IPV4 = re.compile(
    r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])"
)
# Lock-file integrity hashes are long and random but public.
_PUBLIC_HASH = re.compile(r"^sha(?:1|256|384|512)-")

_DIFF_FILE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_PLUS_FILE = re.compile(r"^(?:\+\+\+|---) (?:[ab]/)?(?P<p>.+?)\s*$")
_RENAME = re.compile(r"^(?:rename|copy) (?:from|to) (?P<p>.+)$")


def entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _placeholder(value: str) -> bool:
    v = value.strip("\"'")
    return (
        not v
        or v.startswith(("$", "<", "{{", "%("))
        or v.lower() in {"changeme", "change-me", "xxx", "none", "null", "example", "..."}
        or set(v) <= {"x", "X", "*", "."}
    )


@dataclass(slots=True)
class Classification:
    outcome: Outcome
    reasons: list[str] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    blocked_paths: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reasons": self.reasons,
            "redactions": self.redactions,
            "files": self.files[:200],
            "file_count": len(self.files),
            "blocked_paths": self.blocked_paths,
        }


@dataclass(slots=True)
class Filtered:
    """What may be sent (only meaningful when the outcome is not BLOCKED)."""

    classification: Classification
    title: str
    body: str
    diff: str


def diff_paths(diff: str) -> list[str]:
    """Every path a unified diff touches (both sides of a rename), in order, deduplicated.

    `---`/`+++`/rename lines count only in a file header (before its first hunk), so a
    removed line that happens to start with `-- ` is never taken for a path.
    """
    seen: dict[str, None] = {}
    in_header = False
    for line in diff.splitlines():
        if m := _DIFF_FILE.match(line):
            seen.setdefault(m["a"])
            seen.setdefault(m["b"])
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        elif in_header and (m := _PLUS_FILE.match(line)) and m["p"] != "/dev/null":
            seen.setdefault(m["p"])
        elif in_header and (m := _RENAME.match(line)):
            seen.setdefault(m["p"])
    return list(seen)


def _line_locator(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _where(starts: Sequence[int], pos: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def secret_hits(text: str) -> list[tuple[str, int]]:
    """(rule, 1-based line) for every secret-shaped match in `text`."""
    hits: list[tuple[str, int]] = []
    starts = _line_locator(text)
    for rule, pat in _SECRET_RULES:
        for m in pat.finditer(text):
            hits.append((rule, _where(starts, m.start())))
    for m in _ENV_LINE.finditer(text):
        if not _placeholder(m["value"]):
            hits.append(("env_secret_line", _where(starts, m.start())))
    for m in _NAMED_LITERAL.finditer(text):
        v = m["value"]
        if entropy(v) >= NAMED_ENTROPY and not _placeholder(v):
            hits.append(("high_entropy_assignment", _where(starts, m.start())))
    for m in _ANY_LITERAL.finditer(text):
        if entropy(m["value"]) >= ANY_ENTROPY and not _PUBLIC_HASH.match(m["value"]):
            hits.append(("high_entropy_literal", _where(starts, m.start())))
    hits.sort(key=lambda h: h[1])
    return hits


def _host_pattern(domains: Iterable[str]) -> re.Pattern[str] | None:
    suffixes = {re.escape(d.strip(".").lower()) for d in domains if d.strip(".")}
    if not suffixes:
        return None
    alt = "|".join(sorted(suffixes, key=len, reverse=True))
    return re.compile(rf"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)*(?:{alt})\b")


def redact(text: str, domains: Iterable[str], counts: Counter[str]) -> str:
    """Mask emails, IPv4 addresses and internal hostnames, counting each kind."""

    def masker(kind: str, repl: str) -> Any:
        def f(_m: re.Match[str]) -> str:
            counts[kind] += 1
            return repl

        return f

    text = _EMAIL.sub(masker("email", "[email]"), text)
    text = _IPV4.sub(masker("ip", "[ip]"), text)
    host = _host_pattern(domains)
    if host is not None:
        text = host.sub(masker("internal_host", "[internal-host]"), text)
    return text


class _Sections:
    """Maps a diff line number to the file whose section it is in."""

    def __init__(self, diff: str) -> None:
        self.heads: list[tuple[int, str]] = []
        for i, line in enumerate(diff.splitlines(), start=1):
            if m := _DIFF_FILE.match(line):
                self.heads.append((i, m["b"]))

    def file_at(self, line: int) -> str | None:
        found = None
        for start, path in self.heads:
            if start > line:
                break
            found = path
        return found


def classify(title: str, body: str, diff: str, *, internal_domains: Iterable[str] = ()) -> Filtered:
    """Classify a PR (title, body, unified diff) and return the masked text to send."""
    files = diff_paths(diff)
    reasons: list[str] = []
    blocked = {p: c for p in files if (c := classify_path(p)) is not None}
    for p, cat in blocked.items():
        reasons.append(f"path:{cat}:{p}")
    sections = _Sections(diff)
    for rule, line in secret_hits(diff):
        where = sections.file_at(line) or "diff"
        reasons.append(f"content:{rule}:{where} (diff line {line})")
    for name, text in (("title", title), ("body", body)):
        for rule, line in secret_hits(text):
            reasons.append(f"content:{rule}:pr_{name} (line {line})")
    # dedupe, keep order, cap
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        extra = len(reasons) - MAX_REASONS
        reasons = reasons[:MAX_REASONS] + ([f"… and {extra} more"] if extra > 0 else [])
        verdict = Classification("BLOCKED", reasons, {}, files, blocked)
        return Filtered(verdict, "", "", "")
    counts: Counter[str] = Counter()
    domains = list(internal_domains)
    out_title = redact(title, domains, counts)
    out_body = redact(body, domains, counts)
    out_diff = redact(diff, domains, counts)
    outcome: Outcome = "REDACT" if counts else "ALLOW"
    reds = dict(sorted(counts.items()))
    why = [f"masked:{k}x{v}" for k, v in reds.items()]
    return Filtered(Classification(outcome, why, reds, files, {}), out_title, out_body, out_diff)


__all__ = ["Classification", "Filtered", "classify", "diff_paths", "entropy", "secret_hits"]
