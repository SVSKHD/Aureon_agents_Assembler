#!/usr/bin/env bash
# Migration rollback drill (docs/24-production-integration.md, "Rollback drill").
#
#   scripts/ops/rollback-drill.sh             # the last 3 revisions
#   scripts/ops/rollback-drill.sh --steps 8   # the last 8
#
# 1. creates a scratch database aureon_rbk_<ts> on the Postgres server (an empty one: the
#    live database is NEVER used, not even read);
# 2. inside the migrate image (the DSN never leaves the container):
#    alembic upgrade head -> downgrade -1 (x steps) -> upgrade +1 back to head, a schema
#    snapshot before/after that must be identical, and an app smoke test (every mapped table
#    selectable, version = head);
# 3. writes the JSON report to BACKUP_DIR/drills/rollback-<ts>.json and prints it;
# 4. drops ONLY the scratch database it created (even on failure).
# Exit 0 = every downgrade and upgrade worked and the round trip lost nothing.
set -euo pipefail
umask 077

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STEPS=3
while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps)
      STEPS="${2:-}"
      shift 2
      ;;
    -h | --help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done
[[ "$STEPS" =~ ^[0-9]+$ && "$STEPS" -ge 1 && "$STEPS" -le 200 ]] ||
  die "--steps must be a whole number between 1 and 200" 2
require_cmd docker python3

STAMP="$(date -u +%Y%m%d_%H%M%S)"
SCRATCH_DB="aureon_rbk_$STAMP"
valid_db_name "$SCRATCH_DB" || die "invalid scratch database name" 2
[[ "$SCRATCH_DB" != "$PG_DB" ]] || die "scratch database must differ from the live one" 2
CREATED=0
WORK="$(mktemp -d)"

drop_scratch() {
  local rc=$?
  # Only the database this run created, and only an aureon_rbk_* name.
  if [[ "$CREATED" == 1 && "$SCRATCH_DB" == aureon_rbk_* && "$SCRATCH_DB" != "$PG_DB" ]]; then
    log "dropping scratch database $SCRATCH_DB"
    psql_q postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH_DB\"" ||
      log "WARNING: could not drop $SCRATCH_DB; drop it by hand"
  fi
  rm -rf -- "$WORK"
  exit "$rc"
}
trap drop_scratch EXIT

exists="$(psql_q postgres -c "SELECT 1 FROM pg_database WHERE datname = '$SCRATCH_DB'")"
[[ -z "$exists" ]] || die "$SCRATCH_DB already exists; not touching it" 3
# Owned by the migrator role when the scoped roles are in use (deploy/sql/roles.sql), because
# the migrate image connects as that role (MIGRATIONS_DATABASE_URL) and must create schemas.
owner=()
if [[ -n "${AUREON_MIGRATOR_ROLE:-}" ]]; then
  [[ "$AUREON_MIGRATOR_ROLE" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "invalid AUREON_MIGRATOR_ROLE" 2
  owner=(OWNER "\"$AUREON_MIGRATOR_ROLE\"")
fi
psql_q postgres -c "CREATE DATABASE \"$SCRATCH_DB\" ${owner[*]} TEMPLATE template0"
CREATED=1

log "rollback drill on $SCRATCH_DB: $STEPS step(s)"
rc=0
docker compose --project-directory "$AUREON_DIR" run --rm --no-deps -T migrate \
  python -m app.workers.migrate --rollback-drill "$SCRATCH_DB" --steps "$STEPS" \
  >"$WORK/drill.out" 2>&1 || rc=$?

REPORT_DIR="$BACKUP_DIR/drills"
if [[ -d "$DATA_ROOT" && -f "$DATA_ROOT/$DATA_ROOT_MARKER" ]]; then
  mkdir -p "$REPORT_DIR"
  REPORT="$REPORT_DIR/rollback-$STAMP.json"
else
  REPORT="$WORK/rollback-$STAMP.json" # no HDD: print only
fi
python3 - "$WORK/drill.out" "$REPORT" <<'PY' || rc=1
import json, sys
from pathlib import Path

out = Path(sys.argv[1]).read_text(errors="replace")
marker = "=== aureon rollback drill report ==="
if marker not in out:
    print(out[-4000:], file=sys.stderr)
    sys.exit("no drill report (see output above)")
report = json.loads(out.split(marker, 1)[1])
Path(sys.argv[2]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps(report, indent=2, sort_keys=True))
sys.exit(0 if report.get("passed") else 1)
PY
[[ "$rc" == 0 ]] || die "rollback drill FAILED (report: $REPORT)" 1
log "rollback drill passed; report $REPORT"
