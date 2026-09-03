import logging
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from myapp.Models.Auth_models import UserRole
from myapp.Utils.email_tasks import send_email_async

log = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Backfill verification_deadline (14 days) for existing unverified users and send them a notice.'

    def add_arguments(self, parser):
        parser.add_argument(
            "--resend-notices",
            action="store_true",
            help="Also resend notices to active legacy users that already have a deadline.",
        )
        parser.add_argument(
            "--email",
            action="append",
            default=[],
            help="Process only this email address; repeat for multiple failed recipients.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        now = timezone.now()
        deadline = now + timedelta(days=14)
        
        # Find all active, unverified, non-admin/accountant users who do not have a deadline set yet.
        users_to_backfill = User.objects.filter(
            is_active=True,
            email_verified=False,
        ).exclude(role__in=[UserRole.ADMIN, UserRole.ACCOUNTANT])
        if not options["resend_notices"]:
            users_to_backfill = users_to_backfill.filter(
                verification_deadline__isnull=True,
            )
        if options["email"]:
            users_to_backfill = users_to_backfill.filter(
                email__in=options["email"],
            )
        
        count = users_to_backfill.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('No users to backfill.'))
            return
            
        self.stdout.write(f'Found {count} existing unverified user(s). Backfilling deadline and sending emails...')
        
        # We iterate so we can fire off the email tasks individually
        for user in users_to_backfill:
            if user.verification_deadline is None:
                user.verification_deadline = deadline
                user.save(update_fields=["verification_deadline"])
            
            # Send the notice email
            send_email_async(
                to=[user.email],
                subject="Action Required: Please verify your PaidiX email",
                template="auth/email_verification_notice",
                context={"name": user.full_name or "there"}
            )
            # The central sender enforces the provider limit. This pause also
            # bounds background thread creation during a large manual campaign.
            time.sleep(0.15)
            
        self.stdout.write(self.style.SUCCESS(f'Successfully processed {count} user(s).'))
