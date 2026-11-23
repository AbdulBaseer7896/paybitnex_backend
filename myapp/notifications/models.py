"""
Notification model for persisting events before dispatching over WebSockets.
"""
from django.db import models
from django.utils import timezone


class NotificationType(models.TextChoices):
    PAYMENT_STATUS_UPDATED = "payment_status_updated", "Payment Status Updated"
    PAYMENT_RECEIVED_CONFIRMED = "payment_received_confirmed", "Payment Received Confirmed"


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
