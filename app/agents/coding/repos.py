"""The repo allowlist: CODE_REPOS in settings, nothing else is readable."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


class RepoNotAllowed(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RepoConfig:
    name: str
    path: Path  # absolute path of the main checkout
    base: str  # branch tasks start from (e.g. dev01, main)
    tests: tuple[tuple[str, ...], ...] = ()  # each must be in CODE_ALLOWED_COMMANDS
    # Directories of the main checkout linked (symlink) into each worktree so tests can
    # run offline without an install: node_modules, .venv.
    link: tuple[str, ...] = field(default_factory=tuple)


def parse_repos(raw: dict[str, Any]) -> dict[str, RepoConfig]:
    out: dict[str, RepoConfig] = {}
    for name, spec in raw.items():
        if not NAME.match(name):
            raise ValueError(f"bad repo name {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"repo {name}: expected an object")
        path = Path(str(spec.get("path", "")))
        if not path.is_absolute():
            raise ValueError(f"repo {name}: path must be absolute")
        base = str(spec.get("base", "main"))
        if not BRANCH.match(base) or ".." in base:
            raise ValueError(f"repo {name}: bad base branch")
        tests = tuple(tuple(str(a) for a in argv) for argv in spec.get("tests", []))
        link = tuple(str(d) for d in spec.get("link", []))
        for d in link:
            if "/" in d or d in ("", ".", "..", ".git"):
                raise ValueError(f"repo {name}: link entries are top-level directory names")
        out[name] = RepoConfig(name, path, base, tests, link)
    return out


def get_repo(repos: dict[str, RepoConfig], name: str) -> RepoConfig:
    repo = repos.get(name)
    if repo is None:
        raise RepoNotAllowed(name)
    return repo
