"""SETTINGS_MASTER_KEY maintenance (docs/settings.md). Run inside the api image:

    docker compose run --rm --no-deps -T api python -m app.workers.settings_key check
    docker compose run --rm --no-deps -T api python -m app.workers.settings_key rotate

`check`  decrypts every core.app_settings row with the configured key(s); exit 1 if any fails.
`rotate` re-encrypts every row under the FIRST key of SETTINGS_MASTER_KEY ("new,old"), so the
         old key can then be removed. Prints key names and counts only, never a value or key.
"""

from __future__ import annotations

import asyncio
import sys

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.config import get_env_settings
from app.db.session import build_engine, build_sessionmaker
from app.models.app_settings import AppSetting
from app.services.app_settings import Cipher, SettingsError


async def run(command: str) -> int:
    settings = get_env_settings()
    try:
        cipher = Cipher(settings.SETTINGS_MASTER_KEY)
    except SettingsError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2
    engine = build_engine(settings)
    bad: list[str] = []
    done = 0
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            rows = (await db.scalars(select(AppSetting).with_for_update())).all()
            for row in rows:
                try:
                    if command == "rotate":
                        row.value_encrypted = cipher.rotate(row.value_encrypted)
                    else:
                        cipher.decrypt(row.value_encrypted)
                    done += 1
                except InvalidToken:
                    bad.append(row.key)
            if bad and command == "rotate":
                raise RuntimeError("rollback")  # all or nothing
    except RuntimeError:
        pass
    finally:
        await engine.dispose()
    verb = "rotated" if command == "rotate" and not bad else "readable"
    print(f"{verb}: {done if not bad else 0} of {done + len(bad)} rows")
    if bad:
        print(
            f"undecryptable with the configured key(s): {', '.join(sorted(bad))}", file=sys.stderr
        )
        return 1
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("check", "rotate"):
        print(__doc__, file=sys.stderr)
        return 2
    return asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
