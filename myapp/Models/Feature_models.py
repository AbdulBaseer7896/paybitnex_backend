"""
Feature access control for customers.

A customer can be granted access to premium features by an admin. Every
premium feature has a short string key (e.g. "invoicing"); a row in this
table with `enabled=True` means the customer is allowed to use it.

Basic features (dashboard, my payments, new payment, bank accounts) are
never gated here — they're free for every verified customer and don't
appear in the feature registry at all.

The design intentionally avoids a static schema (no boolean-per-feature
column) so adding a new paid feature later is zero DB work — just add
the key to FEATURES in Utils/features.py and gate the endpoints.
"""
import uuid
from django.db import models


class CustomerFeatureAccess(models.Model):
    """One row per (user, feature_key). Presence + enabled=True → allowed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="feature_grants",
    )
    feature_key = models.CharField(
        max_length=64, db_index=True,
        help_text="String key from the FEATURES registry (e.g. 'invoicing').",
    )
    enabled = models.BooleanField(default=True)
    notes = models.TextField(
        blank=True,
        help_text="Optional admin notes — why access was granted/revoked.",
    )

    granted_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="features_granted",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_feature_access"
        unique_together = [("user", "feature_key")]
        indexes = [
            models.Index(fields=["user", "feature_key"]),
            models.Index(fields=["feature_key", "enabled"]),
        ]

    def __str__(self):
        return (
            f"{self.user.email} · {self.feature_key} · "
            f"{'enabled' if self.enabled else 'disabled'}"
        )
