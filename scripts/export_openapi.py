"""Write the API's OpenAPI schema to stdout (or a file) for the frontend's typed client.

    python scripts/export_openapi.py > openapi.json
    python scripts/export_openapi.py --out ../astra-frontend/src/api/openapi.json

Builds the app with placeholder settings; nothing is connected and no secret is read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.main import create_app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ENV="dev",
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        REDIS_URL="redis://127.0.0.1:1/0",
        OLLAMA_BASE_URL="http://127.0.0.1:1",
        SECRET_KEY="x" * 40,
        DATA_ROOT="/nonexistent",
    )
    schema = create_app(settings).openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
