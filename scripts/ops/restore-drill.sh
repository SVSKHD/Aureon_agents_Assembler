#!/usr/bin/env bash
# Restore drill (docs/16-production-hardening.md, "Restore drill"): proves a backup restores.
#
#   scripts/ops/restore-drill.sh            # the latest backup
#   scripts/ops/restore-drill.sh aureon-20261102T013000Z
#
# 1. restores the backup into a scratch database spasta_drill_<ts> (restore.sh, checksums
#    verified first);
# 2. runs `alembic current` against it (inside the migrate container) and compares the
#    revision with the manifest;
# 3. compares exact row counts of every owned table with the counts taken at backup time;
# 4. writes a JSON report to BACKUP_DIR/drills/ and prints a summary;
# 5. drops ONLY the scratch database it created (even on failure). Never touches the live DB.
# Exit 0 = drill passed.
set -euo pipefail
umask 077

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SRC="${1:-latest}"
[[ $# -le 1 ]] || die "usage: restore-drill.sh [backup]" 2
require_cmd docker python3 realpath

STAMP="$(date -u +%Y%m%d_%H%M%S)"
DRILL_DB="spasta_drill_$STAMP"
valid_db_name "$DRILL_DB" || die "invalid drill database name" 2
[[ "$DRILL_DB" != "$PG_DB" ]] || die "drill database must differ from the live one" 2
CREATED=0
WORK="$(mktemp -d)"

drop_scratch() {
  local rc=$?
  # Only the database this run created, and only a spasta_drill_* name.
  if [[ "$CREATED" == 1 && "$DRILL_DB" == spasta_drill_* && "$DRILL_DB" != "$PG_DB" ]]; then
    log "dropping scratch database $DRILL_DB"
    psql_q postgres -c "DROP DATABASE IF EXISTS \"$DRILL_DB\"" ||
      log "WARNING: could not drop $DRILL_DB; drop it by hand"
  fi
  rm -rf -- "$WORK"
  exit "$rc"
}
trap drop_scratch EXIT

exists="$(psql_q postgres -c "SELECT 1 FROM pg_database WHERE datname = '$DRILL_DB'")"
[[ -z "$exists" ]] || die "$DRILL_DB already exists; not touching it" 3

SRC="$(resolve_backup "$SRC")"
[[ -f "$SRC/MANIFEST.json" ]] || die "not a backup: $SRC" 3

CREATED=1 # from here on the scratch DB may exist and is ours to drop
"$OPS_DIR/restore.sh" "$SRC" --target-db "$DRILL_DB" >"$WORK/restore.out"

log "alembic current on $DRILL_DB"
docker compose --project-directory "$AUREON_DIR" run --rm --no-deps -T migrate \
  python -m app.workers.migrate --current-for "$DRILL_DB" >"$WORK/alembic.out" 2>&1 ||
  log "alembic current failed (see report)"

log "row counts on $DRILL_DB"
row_count_sql | psql_q "$DRILL_DB" -f - >"$WORK/counts.txt"

mkdir -p "$BACKUP_DIR/drills"
REPORT="$BACKUP_DIR/drills/drill-$STAMP.json"
python3 - "$SRC/MANIFEST.json" "$WORK" "$DRILL_DB" "$REPORT" <<'PY'
import json, re, sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
work, db, report = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
alembic_out = (work / "alembic.out").read_text()
revs = re.findall(r"^([0-9a-f]{4,32})\b", alembic_out, re.M)
restored = {}
for line in (work / "counts.txt").read_text().splitlines():
    if "|" in line:
        name, n = line.rsplit("|", 1)
        restored[name] = int(n)
expected = manifest.get("row_counts", {})
mismatches = {
    t: {"expected": n, "restored": restored.get(t)}
    for t, n in expected.items()
    if restored.get(t) != n
}
want_rev = manifest.get("alembic_revision")
result = {
    "backup": manifest.get("created_at"),
    "scratch_database": db,
    "alembic_current": revs[0] if revs else None,
    "alembic_expected": want_rev,
    "tables_checked": len(expected),
    "rows_expected": sum(expected.values()),
    "rows_restored": sum(v for k, v in restored.items() if k in expected),
    "mismatches": mismatches,
}
result["passed"] = bool(revs) and revs[0] == want_rev and not mismatches and bool(expected)
report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(json.dumps(result, indent=2, sort_keys=True))
sys.exit(0 if result["passed"] else 1)
PY
log "drill passed; report $REPORT"
