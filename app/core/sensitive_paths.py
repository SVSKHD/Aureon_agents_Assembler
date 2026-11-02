"""Repo paths that automated agents must never touch or send anywhere.

Shared by the Claude review filter (docs/22-claude-review.md) and meant for the Phase 18
self-repair hard-block list too (one list, so the two cannot drift; see D-19-2).

A path is checked lower-cased, with backslashes as slashes. It is sensitive when:
- its file name or a directory matches a glob in one of the category lists, or
- one of its tokens (the path split on `/ . _ -`) is in that category's token set.

Categories: `secrets`, `infra`, `trading_risk`, `auth`. Self-repair uses
`classify_repair_path`, which adds a few repair-only paths (and the `repair_system`
category) without changing what the review filter flags.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

# Example env files carry no values worth hiding and change in most phases; their content is
# still scanned by the content filter like any other file.
EXAMPLE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

SECRET_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.gpg",
    "*.asc",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.ovpn",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".htpasswd",
    "*secret*",
    "*credential*",
    "*.tfstate",
    "*.tfvars",
)
SECRET_DIRS: tuple[str, ...] = ("secrets", ".secrets", ".ssh", ".gnupg", ".aws", "credentials")

INFRA_GLOBS: tuple[str, ...] = (
    "dockerfile*",
    "*.dockerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "*.tf",
    "*.service",
    "*.timer",
    "nginx*.conf",
    "caddyfile",
    "cloudflared*",
    "*.hcl",
)
INFRA_DIRS: tuple[str, ...] = (
    "deploy",
    "infra",
    "terraform",
    "ansible",
    "k8s",
    "helm",
    "systemd",
    "nginx",
    ".github",  # workflows run with repo secrets
)

TRADING_TOKENS: frozenset[str] = frozenset(
    {
        "trading",
        "trade",
        "trades",
        "risk",
        "risks",
        "broker",
        "brokers",
        "dhan",
        "mt5",
        "order",
        "orders",
        "execution",
        "executions",
        "live",
        "phase_g",
        "paper",
        "positions",
        "position",
    }
)
AUTH_TOKENS: frozenset[str] = frozenset(
    {
        "auth",
        "authn",
        "authz",
        "security",
        "login",
        "password",
        "passwords",
        "oauth",
        "csrf",
        "jwt",
        "rbac",
        "session",
        "sessions",
        "permissions",
        "procsec",
        "prod_checks",
        "approvals",
        "approval",
    }
)

_TOKEN_SPLIT = re.compile(r"[/._\-]+")


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/").lower()


def _tokens(path: str) -> set[str]:
    p = _norm(path)
    toks = {t for t in _TOKEN_SPLIT.split(p) if t}
    # keep the underscore compounds too (phase_g, prod_checks)
    for part in p.split("/"):
        stem = part.rsplit(".", 1)[0]
        toks.add(stem)
    return toks


def _glob_hit(path: str, globs: Iterable[str], dirs: Iterable[str]) -> bool:
    p = _norm(path)
    parts = p.split("/")
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, g) for g in globs):
        return True
    return any(d in parts[:-1] for d in dirs)


def classify_path(path: str) -> str | None:
    """The first sensitive category `path` falls in, or None."""
    p = _norm(path)
    if not p:
        return None
    name = p.split("/")[-1]
    if name not in EXAMPLE_ENV_FILES and _glob_hit(p, SECRET_GLOBS, SECRET_DIRS):
        return "secrets"
    if _glob_hit(p, INFRA_GLOBS, INFRA_DIRS):
        return "infra"
    toks = _tokens(p)
    if toks & TRADING_TOKENS:
        return "trading_risk"
    if toks & AUTH_TOKENS:
        return "auth"
    return None


def sensitive_paths(paths: Iterable[str]) -> dict[str, str]:
    """{path: category} for every sensitive path in `paths`."""
    out: dict[str, str] = {}
    for p in paths:
        cat = classify_path(p)
        if cat is not None:
            out[p] = cat
    return out


# --- self-repair only (docs/21-self-repair.md) -------------------------------------------
# A repair may never write these, on top of everything classify_path() flags. They are kept
# out of classify_path() so the review filter's behaviour does not change: reviewing a
# migration is fine, a model rewriting one is not.
REPAIR_EXTRA_NAME_GLOBS: tuple[tuple[str, str], ...] = (("*key*", "secrets"),)
REPAIR_EXTRA_PREFIXES: tuple[tuple[str, str], ...] = (
    ("app/services/research", "trading_risk"),
    ("scripts/ops/", "infra"),
    ("alembic/", "infra"),
    ("alembic.ini", "infra"),
    ("app/services/repair", "repair_system"),
    ("app/services/incidents", "repair_system"),
    ("app/core/exception_sink", "repair_system"),
    ("app/core/sensitive_paths", "repair_system"),
    ("app/agents/coding/", "repair_system"),
)


def classify_repair_path(path: str) -> str | None:
    """classify_path(), plus the paths only self-repair must never touch."""
    if cat := classify_path(path):
        return cat
    p = _norm(path)
    name = p.split("/")[-1]
    if name in EXAMPLE_ENV_FILES:
        return None
    for glob, cat in REPAIR_EXTRA_NAME_GLOBS:
        if fnmatch.fnmatchcase(name, glob):
            return cat
    for prefix, cat in REPAIR_EXTRA_PREFIXES:
        if p.startswith(prefix):
            return cat
    return None


__all__ = ["EXAMPLE_ENV_FILES", "classify_path", "classify_repair_path", "sensitive_paths"]
