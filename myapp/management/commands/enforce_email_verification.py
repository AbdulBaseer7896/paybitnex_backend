from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from myapp.Models.Auth_models import UserRole

class Command(BaseCommand):
    help = 'Enforce email verification by deactivating unverified users whose grace period has expired.'

    def handle(self, *args, **options):
        User = get_user_model()
        now = timezone.now()
        
        # Query unverified non-admins whose deadline is in the past
        queryset = User.objects.filter(
            email_verified=False,
            verification_deadline__lt=now,
            is_active=True
        ).exclude(role=UserRole.ADMIN)
        
        count = queryset.count()
        if count > 0:
            queryset.update(is_active=False)
            self.stdout.write(self.style.SUCCESS(f'Successfully deactivated {count} unverified user(s).'))
        else:
            self.stdout.write(self.style.SUCCESS('No unverified users with expired grace periods found.'))
