"""
Email-based one-time passcodes used for:
  - Signup verification (purpose='signup')
  - Forgot-password verification (purpose='password_reset')

OTPs are short-lived (60 seconds) and single-use. When verified, we mark
`consumed_at` so the same code can't be replayed.

`attempts` counts failed verifications; we lock out at 5 failed attempts
to deter brute-force guessing of a 6-digit code.
"""
import hmac
import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class OTPPurpose(models.TextChoices):
    SIGNUP        = "signup",        "Signup"
    PASSWORD_RESET = "password_reset", "Password reset"
    EMAIL_VERIFICATION = "email_verification", "Email verification"


def _generate_code() -> str:
    """Secure 6-digit numeric OTP (zero-padded)."""
    return f"{secrets.randbelow(10**6):06d}"


class EmailOTP(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    purpose = models.CharField(
        max_length=20, choices=OTPPurpose.choices, db_index=True,
    )
    code = models.CharField(max_length=6)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "email_otps"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "purpose", "-created_at"]),
        ]

    def __str__(self):
        return f"OTP({self.purpose}, {self.email}, {self.code[:2]}••)"

    # ── Helpers ──────────────────────────────────────────────────
    @classmethod
    def issue(cls, *, email: str, purpose: str, ttl_seconds: int = 60):
        """
        Invalidate any prior outstanding OTPs for this (email, purpose)
        and mint a new one. Returns the new OTP instance.
        """
        # Void existing unconsumed codes to prevent multiple concurrent
        # valid codes per address.
        cls.objects.filter(
            email=email, purpose=purpose, consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())

        return cls.objects.create(
            email=email,
            purpose=purpose,
            code=_generate_code(),
            expires_at=timezone.now() + timedelta(seconds=ttl_seconds),
        )

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_locked(self) -> bool:
        return self.attempts >= 5

    def verify(self, code: str) -> tuple[bool, str]:
        """
        Try the provided code against this OTP.
        Returns (ok, reason) — reason is empty on success, otherwise a
        short machine-friendly error string.
        """
        if self.is_consumed:
            return False, "consumed"
        if self.is_expired:
            return False, "expired"
        if self.is_locked:
            return False, "locked"
        if not hmac.compare_digest(code, self.code):
            self.attempts += 1
            self.save(update_fields=["attempts"])
            return False, "invalid"
        # Success — mark consumed atomically
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])
        return True, ""
