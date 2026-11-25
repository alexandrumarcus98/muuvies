#!/usr/bin/env bash
set -euo pipefail

# Safe online backup of SQLite WAL database + Fernet encryption key with 7-archive rotation.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_DIR="${PROJECT_ROOT}/data"
BACKUP_DIR="${1:-${BACKUP_DIR:-${HOME}/backups/muuvies}}"
KEEP_COUNT="${KEEP_COUNT:-7}"

DB_FILE="${DATA_DIR}/media_search.db"
KEY_FILE="${DATA_DIR}/.fernet_key"

if [ ! -f "${DB_FILE}" ]; then
    echo "Error: SQLite database not found at ${DB_FILE}" >&2
    exit 1
fi

if [ ! -f "${KEY_FILE}" ]; then
    echo "Error: Fernet key not found at ${KEY_FILE}" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_PATH="${BACKUP_DIR}/muuvies_backup_${TIMESTAMP}.tar.gz"

echo "Creating consistent SQLite WAL backup..."
if command -v docker >/dev/null 2>&1 && docker compose -f "${PROJECT_ROOT}/docker-compose.yml" ps --status running -q media-search 2>/dev/null | grep -q .; then
    docker compose -f "${PROJECT_ROOT}/docker-compose.yml" exec -T media-search python3 -c "
import sqlite3
src = sqlite3.connect('/app/data/media_search.db')
dst = sqlite3.connect('/tmp/media_search_backup.db')
src.backup(dst)
dst.close()
src.close()
"
    docker compose -f "${PROJECT_ROOT}/docker-compose.yml" cp media-search:/tmp/media_search_backup.db "${TMP_DIR}/media_search.db"
    docker compose -f "${PROJECT_ROOT}/docker-compose.yml" exec -T media-search rm -f /tmp/media_search_backup.db
elif command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${DB_FILE}" ".backup '${TMP_DIR}/media_search.db'"
else
    python3 -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
" "${DB_FILE}" "${TMP_DIR}/media_search.db"
fi

cp "${KEY_FILE}" "${TMP_DIR}/.fernet_key"
chmod 600 "${TMP_DIR}/media_search.db" "${TMP_DIR}/.fernet_key"

tar -czf "${ARCHIVE_PATH}" -C "${TMP_DIR}" media_search.db .fernet_key
chmod 600 "${ARCHIVE_PATH}"
echo "Saved encrypted-key + DB backup: ${ARCHIVE_PATH}"

# Retain only the newest KEEP_COUNT backups
mapfile -t OLD_BACKUPS < <(ls -1t "${BACKUP_DIR}"/muuvies_backup_*.tar.gz 2>/dev/null | tail -n +"$((KEEP_COUNT + 1))" || true)
if [ "${#OLD_BACKUPS[@]}" -gt 0 ]; then
    rm -f "${OLD_BACKUPS[@]}"
    echo "Rotated ${#OLD_BACKUPS[@]} older backup(s); kept latest ${KEEP_COUNT}."
fi

# Optional off-site upload (e.g. RCLONE_REMOTE="r2:muuvies-backups" or "azure:muuvies-backups")
if [ -n "${RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null 2>&1; then
    echo "Uploading backup to off-site remote ${RCLONE_REMOTE}..."
    rclone copy "${ARCHIVE_PATH}" "${RCLONE_REMOTE}"
fi

