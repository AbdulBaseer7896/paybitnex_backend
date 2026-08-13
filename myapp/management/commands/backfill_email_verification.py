import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from myapp.Models.Auth_models import UserRole
from myapp.Utils.email_tasks import send_email_async

log = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Backfill verification_deadline (14 days) for existing unverified users and send them a notice.'

    def handle(self, *args, **options):
        User = get_user_model()
        now = timezone.now()
        deadline = now + timedelta(days=14)
        
        # Find all active, unverified, non-admin users who do not have a deadline set yet.
        users_to_backfill = User.objects.filter(
            is_active=True,
            email_verified=False,
            verification_deadline__isnull=True
        ).exclude(role=UserRole.ADMIN)
        
        count = users_to_backfill.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('No users to backfill.'))
            return
            
        self.stdout.write(f'Found {count} existing unverified user(s). Backfilling deadline and sending emails...')
        
        # We iterate so we can fire off the email tasks individually
        for user in users_to_backfill:
            user.verification_deadline = deadline
            user.save(update_fields=["verification_deadline"])
            
            # Send the notice email
            send_email_async(
                to=[user.email],
                subject="Action Required: Please verify your PaidiX email",
                template="auth/email_verification_notice",
                context={"name": user.full_name or "there"}
            )
            
        self.stdout.write(self.style.SUCCESS(f'Successfully processed {count} user(s).'))
