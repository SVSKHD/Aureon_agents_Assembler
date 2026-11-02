#!/usr/bin/env bash
# Create / update the scoped Aureon database roles (deploy/sql/roles.sql, docs/24).
#
#   scripts/ops/apply-db-roles.sh             # passwords from env, else prompted (tty)
#   scripts/ops/apply-db-roles.sh --dry-run   # show what would run (passwords masked)
#
# Runs psql INSIDE the Postgres container as PG_USER (the superuser, local socket), against
# PG_DB. Passwords are read from AUREON_APP_DB_PASSWORD, AUREON_MIGRATOR_DB_PASSWORD and
# AUREON_READONLY_DB_PASSWORD (or typed at a prompt) and sent on psql's STDIN as \set lines,
# so they never appear in argv, `ps` or the shell history. An empty password keeps that
# role's current password. Role names: AUREON_APP_ROLE, AUREON_MIGRATOR_ROLE,
# AUREON_READONLY_ROLE, AUREON_AUDIT_WRITER_ROLE (defaults aureon_app, aureon_migrator,
# aureon_readonly, aureon_audit_writer).
# Idempotent; never drops anything. Re-run it after every migration that adds tables.
set -euo pipefail
umask 077

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $arg" 2 ;;
  esac
done

SQL_FILE="$OPS_DIR/../../deploy/sql/roles.sql"
[[ -f "$SQL_FILE" ]] || die "missing $SQL_FILE" 2
valid_db_name "$PG_DB" || die "invalid PG_DB" 2
ROLE_RE='^[a-z_][a-z0-9_]{0,62}$'
PW_RE='^[A-Za-z0-9._~-]{16,200}$'

declare -A ROLES=(
  [app_role]="${AUREON_APP_ROLE:-aureon_app}"
  [migrator_role]="${AUREON_MIGRATOR_ROLE:-aureon_migrator}"
  [readonly_role]="${AUREON_READONLY_ROLE:-aureon_readonly}"
  [audit_writer_role]="${AUREON_AUDIT_WRITER_ROLE:-aureon_audit_writer}"
)
for k in "${!ROLES[@]}"; do
  [[ "${ROLES[$k]}" =~ $ROLE_RE ]] || die "invalid role name for $k" 2
done

declare -A PW_ENV=(
  [app_password]=AUREON_APP_DB_PASSWORD
  [migrator_password]=AUREON_MIGRATOR_DB_PASSWORD
  [readonly_password]=AUREON_READONLY_DB_PASSWORD
)
declare -A PW=()
for k in app_password migrator_password readonly_password; do
  v="${!PW_ENV[$k]:-}"
  if [[ -z "$v" && "$DRY_RUN" == 0 && -t 0 ]]; then
    read -r -s -p "${PW_ENV[$k]} (empty = keep current): " v
    echo >&2
  fi
  if [[ -n "$v" ]]; then
    # token_urlsafe-style only: nothing psql could interpret, no quoting needed.
    [[ "$v" =~ $PW_RE ]] || die "${PW_ENV[$k]}: use 16-200 chars of [A-Za-z0-9._~-]" 2
    PW[$k]="$v"
  fi
done

preamble() { # $1 = 1 to mask passwords
  local k
  for k in app_role migrator_role readonly_role audit_writer_role; do
    printf '\\set %s %s\n' "$k" "${ROLES[$k]}"
  done
  for k in "${!PW[@]}"; do
    if [[ "$1" == 1 ]]; then
      printf "\\\\set %s '***'\n" "$k"
    else
      printf "\\\\set %s '%s'\n" "$k" "${PW[$k]}"
    fi
  done
}

if [[ "$DRY_RUN" == 1 ]]; then
  log "dry run: psql -U $PG_USER -d $PG_DB -f - (inside the Postgres container) with:"
  preamble 1
  log "dry run: then deploy/sql/roles.sql ($(wc -l <"$SQL_FILE") lines)"
  exit 0
fi

log "applying deploy/sql/roles.sql to $PG_DB as $PG_USER"
{
  preamble 0
  cat -- "$SQL_FILE"
} | pg_exec psql -X -q -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" -f -
log "roles applied: ${ROLES[app_role]} ${ROLES[migrator_role]} ${ROLES[readonly_role]} ${ROLES[audit_writer_role]}"
