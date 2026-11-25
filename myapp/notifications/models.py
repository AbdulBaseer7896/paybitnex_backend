"""
Notification model for persisting events before dispatching over WebSockets.
"""
from django.db import models
from django.utils import timezone


class NotificationType(models.TextChoices):
    PAYMENT_STATUS_UPDATED = "payment_status_updated", "Payment Status Updated"
    PAYMENT_RECEIVED_CONFIRMED = "payment_received_confirmed", "Payment Received Confirmed"
    PAYMENT_SUBMITTED = "payment_submitted", "Payment Submitted"
    KYC_STATUS_UPDATED = "kyc_status_updated", "KYC Status Updated"
    KYC_SUBMITTED = "kyc_submitted", "KYC Submitted"


class DeviceType(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"
    WEB = "web", "Web"


class DeviceToken(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        "myapp.User",
        on_delete=models.CASCADE,
        related_name="device_tokens",
        db_index=True,
    )
    token = models.CharField(max_length=512, unique=True, db_index=True)
    device_type = models.CharField(
        max_length=20,
        choices=DeviceType.choices,
        default=DeviceType.ANDROID,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_device_tokens"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user_id} - {self.device_type} ({'active' if self.is_active else 'inactive'})"


class Notification(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.ForeignKey(
        "myapp.User",
        on_delete=models.CASCADE,
        related_name="notifications",
        db_index=True,
    )
    type = models.CharField(
        max_length=50,
        choices=NotificationType.choices,
        db_index=True,
    )
    payment = models.ForeignKey(
        "myapp.IncomingPayment",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
        db_index=True,
    )
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    payload = models.JSONField(
        default=dict,
        help_text="Full event JSON matching the mobile contract",
    )
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"]),
        ]

    def __str__(self):
        return f"Notification #{self.id} for {self.recipient_id} ({self.type})"
