"""
Celery task: remind clients about invoices that have passed their due date.

Scope is deliberately narrow. An invoice is chased only when all of these
hold, which keeps the task from mailing anyone about a document they were
never given in the first place:

  * status is SENT or VIEWED — draft invoices were never delivered, paid
    ones are settled, and void ones are a bookkeeping lie waiting to happen
  * due_date has actually passed
  * it was genuinely emailed to the client (`sent_to_client_at` is set)
  * no reminder has gone out yet (`overdue_reminder_sent_at` is null)

That last condition is what makes this safe to run daily: each invoice is
chased exactly once, ever. Escalating chase-ups are a product decision, not
something a cron job should invent on its own.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="myapp.Utils.invoice_tasks.send_overdue_invoice_reminders")
def send_overdue_invoice_reminders():
    from myapp.Models.Invoicing_models import Invoice, InvoiceStatus
    from myapp.Utils.email_tasks import send_email_async

    today = timezone.localdate()
    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")

    qs = (
        Invoice.objects
        .filter(
            status__in=[InvoiceStatus.SENT, InvoiceStatus.VIEWED],
            due_date__lt=today,
            sent_to_client_at__isnull=False,
            overdue_reminder_sent_at__isnull=True,
        )
        .select_related("customer")
    )

    sent, skipped, reminded_ids = 0, 0, []
    for invoice in qs:
        client_email = (invoice.client_snapshot or {}).get("email")
        if not client_email:
            # Nothing to chase — but stamp it anyway so the query doesn't
            # keep re-examining the same unreachable rows every night.
            skipped += 1
            reminded_ids.append(invoice.id)
            continue

        due_date = invoice.due_date
        if due_date is None:
            skipped += 1
            reminded_ids.append(invoice.id)
            continue

        days_overdue = (today - due_date).days
        due_date_iso = due_date.isoformat()
        try:
            send_email_async(
                to=[client_email],
                subject=f"Reminder: invoice {invoice.number} is overdue",
                template="invoice/overdue_reminder",
                context={
                    "invoice_number": invoice.number,
                    "client_name":    (invoice.client_snapshot or {}).get("name", ""),
                    "company_name":   (invoice.company_snapshot or {}).get("name", ""),
                    "total":          str(invoice.total),
                    "currency_code":  invoice.currency_code,
                    "due_date":       due_date_iso,
                    "days_overdue":   days_overdue,
                    "public_url":     f"{frontend}/invoice/{invoice.share_token}"
                                      if invoice.share_token else "",
                },
                # The supplier chases their own client, so replies must go
                # to them and not into our no-reply mailbox.
                reply_to=[invoice.customer.email] if invoice.customer.email else None,
            )
            sent += 1
            reminded_ids.append(invoice.id)
        except Exception:
            # Leave the stamp off so the next run retries this one.
            logger.exception(
                "overdue reminder failed for invoice %s", invoice.number,
            )

    if reminded_ids:
        Invoice.objects.filter(id__in=reminded_ids).update(
            overdue_reminder_sent_at=timezone.now(),
        )

    logger.info(
        "send_overdue_invoice_reminders: %s reminded, %s stamped without an "
        "address", sent, skipped,
    )
    return {"reminded": sent, "no_client_email": skipped}