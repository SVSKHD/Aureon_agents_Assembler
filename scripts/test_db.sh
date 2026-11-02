#!/usr/bin/env bash
# Create a throwaway database, run the DB tests against it, then drop it.
# Never point this at the real `spasta` database.
#
#   PG_ADMIN_URL=postgresql://postgres:pass@127.0.0.1:5432/postgres scripts/test_db.sh
set -euo pipefail

: "${PG_ADMIN_URL:?set PG_ADMIN_URL to a superuser/createdb URL on a NON-production server}"
DB="aureon_test_$(date +%s)_$$"
if [[ "$DB" == "spasta" ]]; then echo "refusing" >&2; exit 1; fi

psql "$PG_ADMIN_URL" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE \"$DB\""
trap 'psql "$PG_ADMIN_URL" -qc "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)"' EXIT

base="${PG_ADMIN_URL%/*}"
export TEST_DATABASE_URL="${base/postgresql:/postgresql+asyncpg:}/$DB"
pytest -q "$@"
