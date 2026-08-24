"""
Celery task registry for `myapp`.

`app.autodiscover_tasks()` in paybitnex/celery.py imports `<app>.tasks` for
every entry in INSTALLED_APPS — it does NOT walk subpackages. Our tasks live
in `myapp/Utils/*_tasks.py`, so without this module they are only registered
if something else happens to import them first.

That accident is exactly what used to happen: `rate_tasks`, `report_tasks`
and `email_tasks` got pulled in transitively by the view modules Django
loads at startup, while `stale_payment_tasks` was not imported by anything.
Beat published it on schedule and workers rejected it as unregistered.

Re-exporting here makes registration explicit and order-independent. Any new
task module must be added to this list.
"""
from myapp.Utils.digest_tasks import send_daily_ops_digest
from myapp.Utils.email_tasks import cleanup_expired_otps
from myapp.Utils.invoice_tasks import send_overdue_invoice_reminders
from myapp.Utils.rate_tasks import fetch_live_rates
from myapp.Utils.report_tasks import generate_daily_report
from myapp.Utils.stale_payment_tasks import (
    auto_confirm_stale_payments, flag_stale_payments,
)

__all__ = [
    "auto_confirm_stale_payments",
    "cleanup_expired_otps",
    "fetch_live_rates",
    "flag_stale_payments",
    "generate_daily_report",
    "send_daily_ops_digest",
    "send_overdue_invoice_reminders",
]