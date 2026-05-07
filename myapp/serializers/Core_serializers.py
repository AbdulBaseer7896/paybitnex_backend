"""Core serializers: Currency, SystemSetting, AuditLog."""
from rest_framework import serializers
from myapp.Models.Core_models import Currency, SystemSetting
from myapp.Models.Audit_models import AuditLog


class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "is_active", "is_base", "sort_order"]


class SystemSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["key", "value", "description", "updated_at"]
        read_only_fields = ["updated_at"]


# Dynamically discover which optional columns actually exist in the DB.
# The model may declare target_label/metadata but the migration for them
# may not have been applied yet on some deployments.
def _installed_audit_columns():
    try:
        from django.db import connection
        with connection.cursor() as cur:
            desc = connection.introspection.get_table_description(
                cur, AuditLog._meta.db_table,
            )
            return {c.name for c in desc}
    except Exception:
        # Fallback: assume everything the model declares is present.
        return {f.name for f in AuditLog._meta.get_fields()}


_AUDIT_COLUMNS = _installed_audit_columns()

_AUDIT_BASE_FIELDS = [
    "id", "user", "action",
    "target_model", "target_id", "description",
    "before", "after",
    "ip_address", "user_agent",
    "created_at",
]
_AUDIT_OPTIONAL_FIELDS = [f for f in ("target_label", "metadata")
                          if f in _AUDIT_COLUMNS]


class AuditLogSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source="user.email", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = _AUDIT_BASE_FIELDS + ["user_email"] + _AUDIT_OPTIONAL_FIELDS
        read_only_fields = fields


class PaymentMethodFullSerializer(serializers.ModelSerializer):
    """Full serializer including is_default for admin views."""
    class Meta:
        from myapp.Models.Core_models import PaymentMethod
        model = PaymentMethod
        fields = ["code", "label", "is_active", "is_default", "sort_order"]
