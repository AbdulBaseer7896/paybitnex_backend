"""Serializers for the bank-reconciliation audit module."""
from rest_framework import serializers

from myapp.Models.BankAudit_models import BankAudit, BankAuditFile


class BankAuditFileSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = BankAuditFile
        fields = [
            "id", "original_name", "content_type", "size_bytes",
            "file_url", "uploaded_at",
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        if not obj.file:
            return None
        try:
            url = obj.file.url
        except Exception:
            return None
        request = self.context.get("request")
        if request is not None and url and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url


class BankAuditListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for the saved-audit history list.

    Omits the heavy `result` JSON — the list only needs the counters.
    """
    bank_display = serializers.CharField(source="get_bank_display", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    has_file = serializers.SerializerMethodField()

    class Meta:
        model = BankAudit
        fields = [
            "id", "title", "bank", "bank_display",
            "period_start", "period_end",
            "total_statement", "total_system",
            "matched_count", "amount_mismatch_count",
            "only_in_statement_count", "only_in_system_count",
            "created_by_name", "has_file",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_created_by_name(self, obj):
        u = obj.created_by
        if not u:
            return ""
        return getattr(u, "full_name", None) or getattr(u, "email", "") or ""

    def get_has_file(self, obj):
        return obj.files.exists()


class BankAuditDetailSerializer(BankAuditListSerializer):
    """Full serializer — includes the frozen result JSON + files."""
    files = BankAuditFileSerializer(many=True, read_only=True)

    class Meta(BankAuditListSerializer.Meta):
        fields = BankAuditListSerializer.Meta.fields + ["result", "notes", "files"]
        read_only_fields = fields
