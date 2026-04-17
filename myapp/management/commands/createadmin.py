"""
Create an admin user quickly.

Usage:
    python manage.py createadmin --email admin@paybitnex.com --password secret123
    python manage.py createadmin              # interactive prompts
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from getpass import getpass

from myapp.Models.Auth_models import User, UserRole


class Command(BaseCommand):
    help = "Create an admin (superuser) account."

    def add_arguments(self, parser):
        parser.add_argument("--email", type=str, help="Admin email")
        parser.add_argument("--password", type=str, help="Admin password")
        parser.add_argument("--name", type=str, default="Administrator", help="Full name")

    def handle(self, *args, **opts):
        email = opts.get("email") or input("Email: ").strip()
        password = opts.get("password") or getpass("Password: ")
        name = opts.get("name") or "Administrator"

        if not email or not password:
            raise CommandError("Email and password are required.")

        try:
            user = User.objects.create_superuser(
                email=email, password=password, full_name=name,
                role=UserRole.ADMIN, is_profile_complete=True,
            )
        except IntegrityError:
            raise CommandError(f"A user with email {email} already exists.")

        self.stdout.write(self.style.SUCCESS(
            f"Admin created: {user.email} (id={user.id})"
        ))
