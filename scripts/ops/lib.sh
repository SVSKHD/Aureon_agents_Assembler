# shellcheck shell=bash
# Shared helpers for scripts/ops/*.sh. Sourced, never executed.
#
# Configuration comes from the environment, optionally from scripts/ops/ops.env (see
# ops.env.example). Never from the app's .env: these scripts need no application secret.
# Postgres is reached INSIDE its own container (local socket), so no port is published and
# no password crosses the command line.

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ENV_FILE="${OPS_ENV_FILE:-$OPS_DIR/ops.env}"
if [[ -f "$OPS_ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$OPS_ENV_FILE"
fi

DATA_ROOT="${DATA_ROOT:-/mnt/spasta-data}"
DATA_ROOT_MARKER="${DATA_ROOT_MARKER:-.spasta-data-mounted}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_ROOT/backups/aureon}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
# DATA_ROOT subdirectories archived with each backup (missing ones are skipped).
BACKUP_DATA_DIRS="${BACKUP_DATA_DIRS:-images finance-attachments}"
# Functions the legacy finance tables use as column defaults (dumped so a restore into a
# fresh database can recreate those tables).
BACKUP_PUBLIC_FUNCTIONS="${BACKUP_PUBLIC_FUNCTIONS:-astra_uid}"
# This app's compose project (for `docker compose run migrate` in the drill).
AUREON_DIR="${AUREON_DIR:-$(cd "$OPS_DIR/../.." && pwd)}"

# How to reach Postgres. Default: `docker compose exec -T` on the existing private-ai compose
# project. Set PG_CONTAINER instead to use `docker exec -i <container>`.
PG_COMPOSE_DIR="${PG_COMPOSE_DIR:-/opt/private-ai}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_CONTAINER="${PG_CONTAINER:-}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-spasta}"

# Schemas this app owns (alembic/versions) and the public tables it reads or writes.
OWNED_SCHEMAS=(core audit ai agents trading dhan mt5)
PUBLIC_TABLES=(trades expenses secured astra_documents portfolio_profiles aureon_alembic_version)

# shellcheck disable=SC2034  # used by backup.sh
BACKUP_NAME_RE='^aureon-[0-9]{8}T[0-9]{6}Z$'
DB_NAME_RE='^[a-z_][a-z0-9_]{0,62}$'

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
  log "ERROR: $*"
  exit "${2:-1}"
}

utc_stamp() { date -u +%Y%m%dT%H%M%SZ; }

# Run a command inside the Postgres container. stdin is passed through.
pg_exec() {
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -i "$PG_CONTAINER" "$@"
  else
    docker compose --project-directory "$PG_COMPOSE_DIR" exec -T "$PG_SERVICE" "$@"
  fi
}

# psql against a database, unaligned/tuples-only, stop on the first error.
psql_q() {
  local db="$1"
  shift
  pg_exec psql -X -q -At -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$db" "$@"
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
  done
}

# Absolute, symlink-resolved path (the path need not exist yet).
real_path() { realpath -m -- "$1"; }

# True when $1 is strictly inside directory $2 (both resolved).
path_is_under() {
  local child parent
  child="$(real_path "$1")"
  parent="$(real_path "$2")"
  [[ "$parent" != "/" && "$child" != "$parent" && "$child" == "$parent"/* ]]
}

require_hdd() {
  [[ -f "$DATA_ROOT/$DATA_ROOT_MARKER" ]] ||
    die "HDD not mounted: marker $DATA_ROOT/$DATA_ROOT_MARKER is missing" 4
}

# BACKUP_DIR must live on the HDD (under DATA_ROOT) unless explicitly allowed.
require_backup_dir() {
  if [[ "${BACKUP_ALLOW_OUTSIDE_DATA_ROOT:-0}" != "1" ]] && ! path_is_under "$BACKUP_DIR" "$DATA_ROOT"; then
    die "BACKUP_DIR ($BACKUP_DIR) is not under DATA_ROOT ($DATA_ROOT)" 4
  fi
  [[ "$(real_path "$BACKUP_DIR")" != "/" ]] || die "BACKUP_DIR must not be /" 4
}

valid_db_name() { [[ "$1" =~ $DB_NAME_RE ]]; }

# 'a','b','c' from fixed identifiers (constants above; never user input).
sql_list() {
  local IFS=,
  local joined="$*"
  printf "'%s'" "${joined//,/\',\'}"
}

# SQL printing "schema.table|<exact count>" for every table this app owns (psql \gexec).
row_count_sql() {
  local schemas tables
  schemas="$(sql_list "${OWNED_SCHEMAS[@]}")"
  tables="$(sql_list "${PUBLIC_TABLES[@]}")"
  cat <<SQL
SELECT format('SELECT %L || ''|'' || count(*) FROM %I.%I',
              n.nspname || '.' || c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND (n.nspname IN ($schemas) OR (n.nspname = 'public' AND c.relname IN ($tables)))
ORDER BY 1
\\gexec
SQL
}

# Absolute path of a backup folder: "latest", a folder name under BACKUP_DIR, or a path.
resolve_backup() {
  local src="$1"
  if [[ "$src" == "latest" ]]; then
    src="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'aureon-*' \
      -exec test -f '{}/MANIFEST.json' ';' -print 2>/dev/null |
      grep -E '/aureon-[0-9]{8}T[0-9]{6}Z$' | sort | tail -n 1 || true)"
    [[ -n "$src" ]] || die "no backups found in $BACKUP_DIR" 3
  elif [[ "$src" != */* ]]; then
    src="$BACKUP_DIR/$src"
  fi
  real_path "$src"
}
