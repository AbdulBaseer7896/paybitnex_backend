import logging
from django.core.management.base import BaseCommand
from myapp.Models.Core_models import SystemSetting
from myapp.Services.email_monitor_service import fetch_and_store_emails

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Backfill missed bank alert emails by rewinding the IMAP UID watermark."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since-uid",
            type=int,
            default=5000,
            help="Starting IMAP message UID to fetch from (default: 5000).",
        )
        parser.add_argument(
            "--max-emails",
            type=int,
            default=5000,
            help="Maximum number of emails to process in this run (default: 5000).",
        )

    def handle(self, *args, **options):
        since_uid = options["since_uid"]
        max_emails = options["max_emails"]

        current_watermark = SystemSetting.get("bank_email_last_uid")
        self.stdout.write(
            self.style.NOTICE(f"Current bank_email_last_uid watermark: {current_watermark}")
        )
        self.stdout.write(
            self.style.NOTICE(f"Backfilling from UID {since_uid} (max {max_emails} emails)...")
        )

        stored = fetch_and_store_emails(max_per_run=max_emails, since_uid=since_uid)

        new_watermark = SystemSetting.get("bank_email_last_uid")
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully backfilled! Stored {stored} new bank alert emails.\n"
                f"Watermark updated to: {new_watermark}"
            )
        )

