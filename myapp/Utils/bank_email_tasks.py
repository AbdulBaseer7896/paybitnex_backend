"""Celery tasks for bank-email ingestion."""
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def poll_bank_emails(self):
    """Fetch unseen bank-alert emails, store them, and run the matcher."""
    from myapp.Services.email_monitor_service import fetch_and_store_emails
    try:
        stored = fetch_and_store_emails(max_per_run=1000)
        logger.info("Bank email poll stored=%d", stored)
        return {"stored": stored}
    except Exception as exc:
        logger.exception("poll_bank_emails failed")
        raise self.retry(exc=exc)