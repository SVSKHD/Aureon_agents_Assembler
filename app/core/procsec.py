"""Process hardening for workers that run untrusted code as children.

The coding worker runs allowlisted test commands (`npm test`, `pytest`) inside worktrees whose
files a local model just patched, so the test code is untrusted. Children get a scrubbed
environment (app/agents/coding/commands.py), but a same-uid child could still read the
parent's original environment (DATABASE_URL, SECRET_KEY, broker tokens) through
/proc/<ppid>/environ or /proc/<ppid>/mem. Marking the worker non-dumpable makes those files
root-only. Children re-become dumpable on exec, which is harmless: their environ is scrubbed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def _prctl(option: int, arg: int = 0) -> int:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    result: int = libc.prctl(option, arg, 0, 0, 0)
    return result


def make_non_dumpable() -> bool:
    """True when the flag is set (Linux); False elsewhere (no-op)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        return _prctl(PR_SET_DUMPABLE, 0) == 0 and _prctl(PR_GET_DUMPABLE) == 0
    except (OSError, AttributeError):
        return False
