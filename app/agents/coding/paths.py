"""Path safety: a repo-relative path may only name something inside that repo, never
.git and never secrets. Checked lexically AND after resolving symlinks."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

# Never readable or writable through the agent, whatever the repo contains.
BLOCKED = (
    ".git",
    ".git/*",
    "*/.git",
    "*/.git/*",
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "*id_rsa*",
    "*id_ed25519*",
    "secrets/*",
    "*/secrets/*",
    "*.sqlite",
    "*.db",
)


class PathRejected(ValueError):
    pass


def normalize(rel: str) -> str:
    """Lexical checks; returns a clean POSIX relative path ('' for the repo root)."""
    if "\x00" in rel or "\\" in rel:
        raise PathRejected("invalid characters in path")
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise PathRejected("absolute paths are not allowed")
    parts = [x for x in p.parts if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise PathRejected("'..' is not allowed")
    clean = "/".join(parts)
    if clean and is_blocked(clean):
        raise PathRejected(f"{clean} is off limits")
    return clean


def is_blocked(clean: str) -> bool:
    low = clean.lower()
    # .env.example is documentation, not a secret.
    if low.endswith(".env.example"):
        return False
    return any(fnmatch(low, pat) for pat in BLOCKED)


def safe_path(root: Path, rel: str) -> Path:
    """An absolute path inside `root` for `rel`, or PathRejected. Symlinks are resolved, so a
    link pointing outside the repo (or into .git) is refused too."""
    clean = normalize(rel)
    base = root.resolve()
    target = (base / clean).resolve()
    if target != base and base not in target.parents:
        raise PathRejected("path escapes the repository")
    inner = target.relative_to(base).as_posix() if target != base else ""
    if inner and is_blocked(inner):
        raise PathRejected(f"{inner} is off limits")
    return target
