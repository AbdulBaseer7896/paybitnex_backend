"""
Management command to apply migration 0033 columns if they are missing.

Django thinks migration 0033 is applied (it's in django_migrations) but
the actual ALTER TABLE was never run on the database. This command adds
the two missing columns safely using IF NOT EXISTS logic.

Usage:
    python manage.py apply_missing_migration_0033
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Adds kyc_last_resubmit_at and kyc_last_resubmit_changes to customer_profiles if missing."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            # Check which columns actually exist
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'customer_profiles'
                  AND column_name IN ('kyc_last_resubmit_at', 'kyc_last_resubmit_changes')
            """)
            existing = {row[0] for row in cursor.fetchall()}

            added = []

            if "kyc_last_resubmit_at" not in existing:
                cursor.execute("""
                    ALTER TABLE customer_profiles
                    ADD COLUMN kyc_last_resubmit_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
                """)
                added.append("kyc_last_resubmit_at")
                self.stdout.write(self.style.SUCCESS("  Added column: kyc_last_resubmit_at"))
            else:
                self.stdout.write("  Column already exists: kyc_last_resubmit_at")

            if "kyc_last_resubmit_changes" not in existing:
                cursor.execute("""
                    ALTER TABLE customer_profiles
                    ADD COLUMN kyc_last_resubmit_changes JSONB NOT NULL DEFAULT '[]'::jsonb
                """)
                added.append("kyc_last_resubmit_changes")
                self.stdout.write(self.style.SUCCESS("  Added column: kyc_last_resubmit_changes"))
            else:
                self.stdout.write("  Column already exists: kyc_last_resubmit_changes")

        if added:
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. Added {len(added)} column(s): {', '.join(added)}\n"
                "Restart gunicorn now: sudo systemctl restart paybitnex"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nNothing to do — both columns already exist."
            ))
