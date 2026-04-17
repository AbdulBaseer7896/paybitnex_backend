"""Report serializers (daily / weekly / monthly + on-demand ranges)."""
from rest_framework import serializers
from myapp.Models.Report_models import DailyReport, WeeklyReport, MonthlyReport


class _ReportBaseSerializer(serializers.ModelSerializer):
    class Meta:
        fields = [
            "id", "period_start", "period_end",
            "total_transactions", "completed_transactions", "rejected_transactions",
            "received_by_currency", "fees_by_currency",
            "total_pkr_sent", "total_fee_pkr",
            "generated_at",
        ]
        read_only_fields = fields


class DailyReportSerializer(_ReportBaseSerializer):
    class Meta(_ReportBaseSerializer.Meta):
        model = DailyReport
        fields = _ReportBaseSerializer.Meta.fields + ["date"]


class WeeklyReportSerializer(_ReportBaseSerializer):
    class Meta(_ReportBaseSerializer.Meta):
        model = WeeklyReport
        fields = _ReportBaseSerializer.Meta.fields + ["year", "week"]


class MonthlyReportSerializer(_ReportBaseSerializer):
    class Meta(_ReportBaseSerializer.Meta):
        model = MonthlyReport
        fields = _ReportBaseSerializer.Meta.fields + ["year", "month"]


class CustomRangeReportSerializer(serializers.Serializer):
    """Live aggregate for an arbitrary [start, end] date window."""
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    total_transactions = serializers.IntegerField()
    completed_transactions = serializers.IntegerField()
    rejected_transactions = serializers.IntegerField()
    received_by_currency = serializers.DictField()
    fees_by_currency = serializers.DictField()
    total_pkr_sent = serializers.DecimalField(max_digits=20, decimal_places=2)
    total_fee_pkr = serializers.DecimalField(max_digits=20, decimal_places=2)
