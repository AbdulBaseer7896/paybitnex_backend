"""
Management command to clean up 'undefined', 'null', or blank full_name entries in User and CustomerProfile.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from myapp.Models.Auth_models import User
from myapp.Models.Profile_models import CustomerProfile


class Command(BaseCommand):
    help = "Cleans up 'undefined', 'null', or blank full_name values across User and CustomerProfile records."

    def handle(self, *args, **options):
        self.stdout.write("Scanning database for invalid full_name values...")
        user_fixed = 0
        profile_fixed = 0

        with transaction.atomic():
            for user in User.objects.all():
                fn = (user.full_name or "").strip()
                if not fn or fn.lower() in ("undefined", "null", "none", "nan"):
                    # Derive a clean fallback name from email username
                    email_prefix = (user.email or "").split("@")[0].strip()
                    fallback_name = email_prefix.capitalize() if email_prefix else "Customer"
                    
                    user.full_name = fallback_name
                    user.save(update_fields=["full_name"])
                    user_fixed += 1
                    self.stdout.write(f"Updated User {user.email}: full_name set to '{fallback_name}'")

            for profile in CustomerProfile.objects.all():
                fn = (profile.full_name or "").strip()
                if not fn or fn.lower() in ("undefined", "null", "none", "nan"):
                    user_fn = (profile.user.full_name or "").strip()
                    if user_fn and user_fn.lower() not in ("undefined", "null", "none", "nan"):
                        fallback_name = user_fn
                    else:
                        email_prefix = (profile.user.email or "").split("@")[0].strip()
                        fallback_name = email_prefix.capitalize() if email_prefix else "Customer"

                    profile.full_name = fallback_name
                    profile.save(update_fields=["full_name"])
                    profile_fixed += 1
                    self.stdout.write(f"Updated CustomerProfile for {profile.user.email}: full_name set to '{fallback_name}'")

        self.stdout.write(self.style.SUCCESS(
            f"Successfully cleaned up {user_fixed} User records and {profile_fixed} CustomerProfile records."
        ))
