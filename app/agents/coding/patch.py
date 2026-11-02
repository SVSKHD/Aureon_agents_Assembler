"""Turning a model's answer into a patch that is safe to apply.

The model is asked for a unified diff. Before `git apply --check` ever sees it, every path
it touches is checked with the same rules as reads (paths.normalize: no absolute paths, no
'..', never .git or secrets), and binary patches, symlinks, submodules and mode changes to
executables are refused. Size is capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.coding.paths import PathRejected, normalize

FENCE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.S)
HEADER = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(\S.*?)(?:\t.*)?$")
DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
RENAME = re.compile(r"^(?:rename|copy) (?:from|to) (\S.*)$")
FORBIDDEN_LINES = (
    "GIT binary patch",
    "Binary files ",
    "new file mode 120000",  # symlink
    "new mode 120000",
    "new file mode 160000",  # submodule
    "new mode 160000",
    "new mode 100755",
    "new file mode 100755",
)


class PatchRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Patch:
    text: str
    files: tuple[str, ...]


def extract(answer: str) -> str:
    """The diff inside the first ```diff fence, or the answer itself if it is a bare diff."""
    m = FENCE.search(answer)
    body = m.group(1) if m else answer
    body = body.strip("\n")
    start = re.search(r"^(diff --git |--- )", body, re.M)
    if not start:
        raise PatchRejected("the model did not return a unified diff")
    return body[start.start() :] + "\n"


def validate(text: str, max_bytes: int) -> Patch:
    if len(text.encode()) > max_bytes:
        raise PatchRejected(f"patch is larger than {max_bytes} bytes")
    files: list[str] = []
    for line in text.splitlines():
        for bad in FORBIDDEN_LINES:
            if line.startswith(bad):
                raise PatchRejected(f"refused: {bad.strip()}")
        paths: list[str] = []
        if m := DIFF_GIT.match(line):
            paths = [m.group(1), m.group(2)]
        elif line.startswith(("--- ", "+++ ")) and (m := HEADER.match(line)):
            paths = [m.group(1)]
        elif m := RENAME.match(line):
            paths = [m.group(1)]
        for p in paths:
            if p == "/dev/null":
                continue
            try:
                clean = normalize(p)
            except PathRejected as exc:
                raise PatchRejected(f"{p}: {exc}") from exc
            if not clean:
                raise PatchRejected("patch targets the repository root")
            if clean not in files:
                files.append(clean)
    if not files:
        raise PatchRejected("patch touches no files")
    return Patch(text, tuple(files))
