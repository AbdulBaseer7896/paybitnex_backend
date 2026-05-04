root@PayBitnex:~/paybitnex_backend/scripts# cat backup.sh
#!/bin/bash
set -a
# Source the .env file
[ -f /root/paybitnex_backend/.env ] && . /root/paybitnex_backend/.env
set +a

TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
BACKUP_DIR="/root/paybitnex_backend/backups"
FILE="$BACKUP_DIR/django_db_$TIMESTAMP.dump"

export PGPASSWORD="postgres"

echo "[$TIMESTAMP] Starting backup..."
pg_dump -h localhost -U postgres -F c django_db > "$FILE"

echo "[$TIMESTAMP] Syncing to S3..."
# CHANGED: Using AWS_STORAGE_BUCKET_NAME to match your .env
rclone copy "$FILE" :s3:"$AWS_STORAGE_BUCKET_NAME"/backups \
  --s3-provider=AWS \
  --s3-access-key-id="$AWS_ACCESS_KEY_ID" \
  --s3-secret-access-key="$AWS_SECRET_ACCESS_KEY" \
  --s3-region="$AWS_REGION" \
  --use-mmap

# Clean up local backups older than 7 days
find "$BACKUP_DIR" -type f -name "*.dump" -mtime +7 -delete

echo "[$TIMESTAMP] Backup and Sync completed successfully."