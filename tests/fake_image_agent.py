"""Stand-in for /opt/image-agent's entrypoint, used by tests of SubprocessGenerator.

Self-contained (stdlib only): the generator runs it with a minimal environment.
"""

import argparse
import json
import os
import struct
import sys
import time
import zlib


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


p = argparse.ArgumentParser()
p.add_argument("--prompt", required=True)
p.add_argument("--steps", type=int, default=5)
p.add_argument("--size", default="64x64")
p.add_argument("--out", required=True)
p.add_argument("--mode", default="ok", choices=["ok", "fail", "hang", "nofile"])
p.add_argument("--dump", default="")
a = p.parse_args()

if a.dump:
    with open(a.dump, "w") as f:
        json.dump({"prompt": a.prompt, "env": sorted(os.environ), "pid": os.getpid()}, f)

for i in range(1, a.steps + 1):
    sys.stdout.write(f"\rsampling: {i}/{a.steps}")
    sys.stdout.flush()
    time.sleep(0.01)
print()

if a.mode == "fail":
    print("RuntimeError: out of memory", file=sys.stderr)
    sys.exit(3)
if a.mode == "hang":
    time.sleep(600)
if a.mode == "ok":
    w, h = (int(n) for n in a.size.split("x"))
    with open(a.out, "wb") as f:
        f.write(png(w, h))
