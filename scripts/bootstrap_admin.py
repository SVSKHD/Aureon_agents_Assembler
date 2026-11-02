#!/usr/bin/env python3
"""Create the first admin. Reads the password from stdin; refuses if an admin exists.

On the server (inside the API image, which has the DB network and .env):

    docker compose run --rm -it migrate python -m app.auth.bootstrap --email you@example.com

From a dev checkout:

    python scripts/bootstrap_admin.py --email you@example.com
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.bootstrap import _main

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    raise SystemExit(asyncio.run(_main(parser.parse_args().email)))
