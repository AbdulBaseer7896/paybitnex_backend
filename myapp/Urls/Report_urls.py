"""Report URLs — stored aggregates + on-demand ranges + closing reports."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Report_views import (
    DailyReportViewSet, WeeklyReportViewSet, MonthlyReportViewSet,
    today_report, yesterday_report, this_week_report, this_month_report,
    quarterly_report, yearly_report, last_months_report,
    custom_range_report, regenerate_reports,
)
from myapp.Views.Closing_report_views import (
    closing_report, closing_report_csv, closing_report_pdf,
)

router = DefaultRouter()
router.register(r"daily", DailyReportViewSet, basename="reports-daily")
router.register(r"weekly", WeeklyReportViewSet, basename="reports-weekly")
router.register(r"monthly", MonthlyReportViewSet, basename="reports-monthly")

urlpatterns = [
    path("", include(router.urls)),
    path("today/", today_report, name="reports-today"),
    path("yesterday/", yesterday_report, name="reports-yesterday"),
    path("this-week/", this_week_report, name="reports-this-week"),
    path("this-month/", this_month_report, name="reports-this-month"),
    path("quarterly/", quarterly_report, name="reports-quarterly"),
    path("yearly/", yearly_report, name="reports-yearly"),
    path("last-months/", last_months_report, name="reports-last-months"),
    path("custom/", custom_range_report, name="reports-custom"),
    path("regenerate/", regenerate_reports, name="reports-regenerate"),
    # New: closing reports (period-based profit / ledger / expense aggregation)
    path("closing/", closing_report, name="reports-closing"),
    path("closing.csv", closing_report_csv, name="reports-closing-csv"),
    path("closing.pdf", closing_report_pdf, name="reports-closing-pdf"),
]
