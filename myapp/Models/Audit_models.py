"""
Audit log — every sensitive action recorded.

Populated by AuditMiddleware on write requests, plus explicit
`AuditLog.record()` calls in services.
"""
import uuid
from django.db import models


class AuditLog(models.Model):
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"
    ACTION_LOGIN = "login"
    ACTION_LOGOUT = "logout"
    ACTION_STATE_CHANGE = "state_change"
    ACTION_RATE_CHANGE = "rate_change"
    ACTION_FEE_CHANGE = "fee_change"
    ACTION_KYC_REVIEW = "kyc_review"
    ACTION_PAYMENT_VERIFY = "payment_verify"
    ACTION_PASSWORD_RESET = "password_reset"
    ACTION_TOGGLE_ACTIVE = "toggle_active"
    ACTION_CHOICES = [
        (ACTION_CREATE, "Create"),
        (ACTION_UPDATE, "Update"),
        (ACTION_DELETE, "Delete"),
        (ACTION_LOGIN, "Login"),
        (ACTION_LOGOUT, "Logout"),
        (ACTION_STATE_CHANGE, "State change"),
        (ACTION_RATE_CHANGE, "Rate change"),
        (ACTION_FEE_CHANGE, "Fee change"),
        (ACTION_KYC_REVIEW, "KYC review"),
        (ACTION_PAYMENT_VERIFY, "Payment verification"),
        (ACTION_PASSWORD_RESET, "Password reset"),
        (ACTION_TOGGLE_ACTIVE, "Toggle active"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    target_model = models.CharField(max_length=80, blank=True, db_index=True)
    target_id = models.CharField(max_length=80, blank=True, db_index=True)
    target_label = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Human-readable short label of the target object at log time.",
    )
    description = models.TextField(blank=True)

    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(
        null=True, blank=True,
        help_text="Request path, extra context, etc.",
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["target_model", "target_id"]),
        ]

    def __str__(self):
        u = self.user.email if self.user else "system"
        return f"[{self.action}] {u} → {self.target_model}:{self.target_id}"

    @classmethod
    def record(cls, *, user, action, target=None, target_label="", description="",
               before=None, after=None, metadata=None, ip=None, ua=""):
        kwargs = dict(
            user=user, action=action, description=description,
            target_label=target_label or "",
            before=before, after=after, metadata=metadata,
            ip_address=ip, user_agent=ua,
        )
        if target is not None:
            kwargs["target_model"] = target.__class__.__name__
            kwargs["target_id"] = str(target.pk)
            if not kwargs["target_label"]:
                kwargs["target_label"] = str(target)[:200]
        return cls.objects.create(**kwargs)

    @classmethod
    async def arecord(cls, *, user, action, target=None, target_label="",
                       description="", before=None, after=None, metadata=None,
                       ip=None, ua=""):
        kwargs = dict(
            user=user, action=action, description=description,
            target_label=target_label or "",
            before=before, after=after, metadata=metadata,
            ip_address=ip, user_agent=ua,
        )
        if target is not None:
            kwargs["target_model"] = target.__class__.__name__
            kwargs["target_id"] = str(target.pk)
            if not kwargs["target_label"]:
                kwargs["target_label"] = str(target)[:200]
        return await cls.objects.acreate(**kwargs)
