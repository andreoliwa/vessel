#!/usr/bin/env bash
# To add this backup to the crontab:
# - crontab -e
# - Add this line: 0 5 * * * /root/vessel/postgres/cron-backup.sh <DATABASE>
# - crontab -l

# $HOME doesn't work in crontab, so we need to set it manually
HOME_DIR=$(dirname "$(dirname "$(dirname "$(realpath "$0")")")")
PG_BACKUP_DIR="${BACKUP_DIR:-$HOME_DIR/OneDrive/Backup}/$(hostname)/postgres14"
mkdir -p "$PG_BACKUP_DIR"

DATABASE=$1
if [ -z "$DATABASE" ]; then
    echo "No database name provided"
    exit 1
fi
echo "Database name: $DATABASE"

echo "Remove empty backups"
find "$PG_BACKUP_DIR" -type f -size 0 -print -delete

echo "Delete old backups for $DATABASE (keep 30)"
ls -t "$PG_BACKUP_DIR/${DATABASE}_"*.sql.gz 2>/dev/null | tail -n +31 | xargs rm -f

set -e
# This file has the environment variables needed to connect to the database
# shellcheck source=/dev/null
# TODO: fragile way of loading env vars in cron; this should be more robust and not a shell script
source "$HOME_DIR/.config/shell.d/01-env-vessel-my-den.sh"
OUTPUT_SQL_FILE="${PG_BACKUP_DIR}/${DATABASE}_$(date "+%Y-%m-%d-%H-%M-%S").sql"
echo "Dumping the database to ${OUTPUT_SQL_FILE}..."
COMMAND="$(which docker) compose \
    -f $HOME_DIR/vessel/postgres/compose.yaml \
    exec -T postgres14 pg_dump -U postgres $DATABASE"
echo "Running command: $COMMAND"
$COMMAND >"$OUTPUT_SQL_FILE"

echo "Compressing the backup file..."
gzip "$OUTPUT_SQL_FILE"
echo "Backup compressed to ${OUTPUT_SQL_FILE}.gz"

ls -lhtr "${PG_BACKUP_DIR}"
