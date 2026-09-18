#!/bin/bash
set -e

# Load environment variables from /opt/paybitnex_backend/.env
[ -f /opt/paybitnex_backend/.env ] && . /opt/paybitnex_backend/.env

# Configuration mapping
S3_BUCKET="$AWS_STORAGE_BUCKET_NAME"
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB="${DB_NAME:-django_db}"
BACKUP_DIR="/opt/paybitnex_backend/backups"

mkdir -p "$BACKUP_DIR"

# Connection string for rclone (matches backup.sh setup)
REMOTE_BASE=":s3:$S3_BUCKET/backups"
RCLONE_FLAGS="--s3-provider=AWS --s3-access-key-id=$AWS_ACCESS_KEY_ID --s3-secret-access-key=$AWS_SECRET_ACCESS_KEY --s3-region=$AWS_REGION"

echo "=========================================="
echo "      PAYBITNEX DATABASE RESTORE          "
echo "=========================================="
echo "1) Recover from LOCAL folder ($BACKUP_DIR)"
echo "2) Recover from CLOUD (S3 Bucket)"
echo "------------------------------------------"

read -p "Select an option [1-2]: " CHOICE

case $CHOICE in
    1)
        echo "Fetching 10 latest LOCAL backups..."
        FILES=$(ls -1t $BACKUP_DIR/*.dump 2>/dev/null | head -n 10 || true)

        if [ -z "$FILES" ]; then
            echo "ERROR: No local backup files found!"
            exit 1
        fi

        echo "------------------------------------------"
        echo "$FILES" | sed 's|.*/||'
        echo "------------------------------------------"

        read -p "Enter the FILENAME to restore: " FILENAME
        SELECTED_FILE="$BACKUP_DIR/$FILENAME"
        ;;

    2)
        echo "Fetching 10 latest CLOUD backups..."
        FILES=$(rclone lsf $REMOTE_BASE $RCLONE_FLAGS | tail -n 10 || true)

        if [ -z "$FILES" ]; then
            echo "ERROR: No backups found in S3!"
            exit 1
        fi

        echo "------------------------------------------"
        echo "$FILES"
        echo "------------------------------------------"

        read -p "Enter the filename to download and restore: " CLOUD_FILE
        echo "Downloading $CLOUD_FILE from S3..."

        rclone copyto $REMOTE_BASE/$CLOUD_FILE $BACKUP_DIR/$CLOUD_FILE $RCLONE_FLAGS

        SELECTED_FILE="$BACKUP_DIR/$CLOUD_FILE"
        ;;

    *)
        echo "Invalid option."
        exit 1
        ;;
esac

# Final validation
if [ ! -f "$SELECTED_FILE" ]; then
    echo "ERROR: File $SELECTED_FILE does not exist!"
    exit 1
fi

echo "------------------------------------------"
echo "READY TO RESTORE: $SELECTED_FILE"
echo "TARGET DATABASE: $POSTGRES_DB"
echo "WARNING: This will overwrite ALL current data."
echo "------------------------------------------"

read -p "Type 'yes' to confirm: " CONFIRM   

if [ "$CONFIRM" = "yes" ]; then
    echo "Restoring... please wait."

    export PGPASSWORD="${DB_PASSWORD:-postgres}"
    pg_restore \
        -h "${DB_HOST:-localhost}" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --clean \
        --no-owner \
        "$SELECTED_FILE"

    echo "SUCCESS: Database recovered."
else
    echo "Aborted."
fi
