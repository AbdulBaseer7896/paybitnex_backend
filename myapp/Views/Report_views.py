"""
Report views.

Stored reports:
    GET /reports/daily/
    GET /reports/weekly/
    GET /reports/monthly/

Live (on-demand, aggregated at request time):
    GET /reports/today/
    GET /reports/yesterday/
    GET /reports/this-week/
    GET /reports/this-month/
    GET /reports/quarterly/?quarter=1&year=2026
    GET /reports/yearly/?year=2026
    GET /reports/custom/?start=YYYY-MM-DD&end=YYYY-MM-DD
    GET /reports/last-months/?months=3  (or 6, 9, 12)

Admin trigger:
    POST /reports/regenerate/
"""
from calendar import monthrange
from datetime import date, timedelta
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Report_models import DailyReport, WeeklyReport, MonthlyReport
from myapp.serializers.Report_serializers import (
    DailyReportSerializer, WeeklyReportSerializer, MonthlyReportSerializer,
    CustomRangeReportSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.report_tasks import _aggregate_range


class DailyReportViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = DailyReport.objects.all()
    serializer_class = DailyReportSerializer
    filterset_fields = ["date"]


class WeeklyReportViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = WeeklyReport.objects.all()
    serializer_class = WeeklyReportSerializer
    filterset_fields = ["year", "week"]


class MonthlyReportViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = MonthlyReport.objects.all()
    serializer_class = MonthlyReportSerializer
    filterset_fields = ["year", "month"]


def _respond_range(start: date, end: date):
    data = _aggregate_range(start, end)
    return Response(CustomRangeReportSerializer(data).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def today_report(request):
    today = timezone.localdate()
    return _respond_range(today, today)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def yesterday_report(request):
    y = timezone.localdate() - timedelta(days=1)
    return _respond_range(y, y)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def this_week_report(request):
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    return _respond_range(monday, today)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def this_month_report(request):
    today = timezone.localdate()
    first = date(today.year, today.month, 1)
    return _respond_range(first, today)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def quarterly_report(request):
    try:
        quarter = int(request.query_params.get("quarter", 1))
        year = int(request.query_params.get("year", timezone.localdate().year))
    except (TypeError, ValueError):
        return Response({"detail": "quarter/year must be integers."},
                        status=status.HTTP_400_BAD_REQUEST)
    if quarter not in (1, 2, 3, 4):
        return Response({"detail": "quarter must be 1..4."},
                        status=status.HTTP_400_BAD_REQUEST)
    start_month = 3 * (quarter - 1) + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    end = date(year, end_month, monthrange(year, end_month)[1])
    return _respond_range(start, end)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def yearly_report(request):
    try:
        year = int(request.query_params.get("year", timezone.localdate().year))
    except (TypeError, ValueError):
        return Response({"detail": "year must be integer."},
                        status=status.HTTP_400_BAD_REQUEST)
    return _respond_range(date(year, 1, 1), date(year, 12, 31))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def last_months_report(request):
    """?months=3|6|9|12 → last N months ending today."""
    try:
        months = int(request.query_params.get("months", 3))
    except (TypeError, ValueError):
        return Response({"detail": "months must be integer."},
                        status=status.HTTP_400_BAD_REQUEST)
    if months < 1 or months > 24:
        return Response({"detail": "months 1..24"},
                        status=status.HTTP_400_BAD_REQUEST)
    end = timezone.localdate()
    y, m = end.year, end.month - months
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    return _respond_range(start, end)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def custom_range_report(request):
    try:
        start = date.fromisoformat(request.query_params["start"])
        end = date.fromisoformat(request.query_params["end"])
    except (KeyError, ValueError):
        return Response(
            {"detail": "Provide start=YYYY-MM-DD&end=YYYY-MM-DD"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if end < start:
        return Response({"detail": "end must be ≥ start"},
                        status=status.HTTP_400_BAD_REQUEST)
    return _respond_range(start, end)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def regenerate_reports(request):
    """Queue regeneration of yesterday's daily + current week/month."""
    from myapp.Utils.report_tasks import (
        generate_daily_report, generate_weekly_report, generate_monthly_report,
    )
    today = timezone.localdate()
    y = today - timedelta(days=1)
    try:
        generate_daily_report.delay(for_date=y.isoformat())
        iso = today.isocalendar()
        generate_weekly_report.delay(iso.year, iso.week)
        generate_monthly_report.delay(today.year, today.month)
    except Exception:
        # If Celery unavailable, run inline
        generate_daily_report.apply(kwargs={"for_date": y.isoformat()}, throw=False)
        iso = today.isocalendar()
        generate_weekly_report.apply(args=(iso.year, iso.week), throw=False)
        generate_monthly_report.apply(args=(today.year, today.month), throw=False)
    return Response({"detail": "Report regeneration queued."})
