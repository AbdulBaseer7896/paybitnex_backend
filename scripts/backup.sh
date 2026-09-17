#!/bin/bash
set -a
# Source the .env file
[ -f /opt/paybitnex_backend/.env ] && . /opt/paybitnex_backend/.env
set +a

TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
BACKUP_DIR="/opt/paybitnex_backend/backups"
FILE="$BACKUP_DIR/django_db_$TIMESTAMP.dump"

mkdir -p "$BACKUP_DIR"  

export PGPASSWORD="${DB_PASSWORD:-postgres}"

echo "[$TIMESTAMP] Starting backup..."
pg_dump -h "${DB_HOST:-localhost}" -U "${DB_USER:-postgres}" -F c "${DB_NAME:-django_db}" > "$FILE"

echo "[$TIMESTAMP] Syncing to S3..."
rclone copy "$FILE" :s3:"$AWS_STORAGE_BUCKET_NAME"/backups \
  --s3-provider=AWS \
  --s3-access-key-id="$AWS_ACCESS_KEY_ID" \
  --s3-secret-access-key="$AWS_SECRET_ACCESS_KEY" \
  --s3-region="$AWS_REGION" \
  --use-mmap

# Clean up local backups older than 7 days
find "$BACKUP_DIR" -type f -name "*.dump" -mtime +7 -delete

echo "[$TIMESTAMP] Backup and Sync completed successfully."
