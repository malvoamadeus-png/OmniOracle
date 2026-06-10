#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="${1:-$ROOT_DIR/supabase/schema.sql}"
CACHE_DIR="${ROOT_DIR}/tmp/portable-psql"

if [[ ! -f "$SQL_FILE" ]]; then
  echo "SQL file not found: $SQL_FILE" >&2
  exit 1
fi

read_env_var() {
  local key="$1"
  local env_file="$ROOT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  grep -E "^${key}=" "$env_file" | tail -n 1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

SUPABASE_DB_URL="${SUPABASE_DB_URL:-$(read_env_var SUPABASE_DB_URL || true)}"
if [[ -z "${SUPABASE_DB_URL:-}" ]]; then
  echo "SUPABASE_DB_URL is required (env or .env)." >&2
  exit 1
fi

find_system_psql() {
  if command -v psql >/dev/null 2>&1; then
    command -v psql
    return 0
  fi
  return 1
}

download_deb() {
  local pkg="$1"
  local out="$2"
  local filename
  filename="$(apt-cache show "$pkg" 2>/dev/null | awk '/^Filename: /{print $2; exit}')"
  if [[ -z "$filename" ]]; then
    echo "Unable to resolve apt filename for package: $pkg" >&2
    return 1
  fi

  local base
  for base in "https://archive.ubuntu.com/ubuntu" "https://security.ubuntu.com/ubuntu"; do
    if curl -fsSL "${base}/${filename}" -o "$out"; then
      return 0
    fi
  done

  echo "Failed to download package: $pkg" >&2
  return 1
}

bootstrap_portable_psql() {
  local root="${CACHE_DIR}/root"
  local bin="${root}/usr/lib/postgresql/16/bin/psql"
  if [[ -x "$bin" ]]; then
    echo "$bin"
    return 0
  fi

  rm -rf "$CACHE_DIR"
  mkdir -p "${CACHE_DIR}/deb" "$root"

  download_deb "postgresql-client-16" "${CACHE_DIR}/deb/postgresql-client-16.deb"
  download_deb "libpq5" "${CACHE_DIR}/deb/libpq5.deb"

  dpkg -x "${CACHE_DIR}/deb/postgresql-client-16.deb" "$root"
  dpkg -x "${CACHE_DIR}/deb/libpq5.deb" "$root"

  if [[ ! -x "$bin" ]]; then
    echo "Portable psql bootstrap failed." >&2
    return 1
  fi

  echo "$bin"
}

if PSQL_BIN="$(find_system_psql 2>/dev/null)"; then
  echo "Using system psql: $PSQL_BIN"
  exec "$PSQL_BIN" "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "$SQL_FILE"
fi

PSQL_BIN="$(bootstrap_portable_psql)"
LIB_DIR="${CACHE_DIR}/root/usr/lib/x86_64-linux-gnu"

echo "Using portable psql: $PSQL_BIN"
exec env LD_LIBRARY_PATH="${LIB_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
  "$PSQL_BIN" "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "$SQL_FILE"
