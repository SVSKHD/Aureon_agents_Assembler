#!/usr/bin/env bash
# Rotate SETTINGS_MASTER_KEY (docs/settings.md). Never prints a key or a value.
#
#   scripts/ops/rotate-settings-key.sh            # 1. prepend a new key ("new,old"), recreate
#                                                  #    the containers, re-encrypt every row
#   scripts/ops/rotate-settings-key.sh --finish   # 2. after `check` passes: drop the old key
#   scripts/ops/rotate-settings-key.sh --init     # first install: add a key if none is set
#
# The .env stays mode 600; a timestamped copy is kept beside it until you delete it.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || { echo "no $ENV_FILE here" >&2; exit 1; }

newkey() { # generated in the api image (it has cryptography); captured, never echoed
  docker compose run --rm --no-deps -T api \
    python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
}

current() { sed -n 's/^SETTINGS_MASTER_KEY=//p' "$ENV_FILE" | tail -n 1; }

write() { # $1 = new value for SETTINGS_MASTER_KEY
  local tmp
  cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date -u +%Y%m%d%H%M%S)"
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  chmod 600 "$tmp"
  grep -v '^SETTINGS_MASTER_KEY=' "$ENV_FILE" >"$tmp" || true
  printf 'SETTINGS_MASTER_KEY=%s\n' "$1" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

recreate() { docker compose up -d --no-build >/dev/null; echo "containers recreated"; }

case "${1:-}" in
  --init)
    if [[ -n "$(current)" ]]; then echo "SETTINGS_MASTER_KEY already set; nothing to do"; exit 0; fi
    key="$(newkey)"; [[ ${#key} -eq 44 ]] || { echo "key generation failed" >&2; exit 1; }
    write "$key"; echo "SETTINGS_MASTER_KEY added to $ENV_FILE";;
  --finish)
    cur="$(current)"; [[ "$cur" == *,* ]] || { echo "only one key configured; nothing to drop"; exit 0; }
    docker compose run --rm --no-deps -T api python -m app.workers.settings_key check
    write "${cur%%,*}"; recreate; echo "old key removed";;
  "")
    cur="$(current)"; [[ -n "$cur" ]] || { echo "no SETTINGS_MASTER_KEY yet: use --init" >&2; exit 1; }
    key="$(newkey)"; [[ ${#key} -eq 44 ]] || { echo "key generation failed" >&2; exit 1; }
    write "$key,$cur"; recreate
    docker compose run --rm --no-deps -T api python -m app.workers.settings_key rotate
    echo "rotated; run with --finish to drop the old key";;
  *) sed -n '2,9p' "$0"; exit 2;;
esac
