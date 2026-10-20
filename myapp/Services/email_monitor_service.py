"""
email_monitor_service.py — IMAP fetch + parse + store.

Called by the Celery task `poll_bank_emails` every 60s. Idempotent: uses
`BankAlertEmail.message_id` as the dedupe key, so re-fetching the same
email never creates duplicates.

Configuration lives in settings.BANK_EMAIL_IMAP (see Part 4).
"""

from __future__ import annotations
import email
import imaplib
import logging
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from myapp.Models.BankEmail_models import BankAlertEmail, BankAlertStatus
from myapp.Models.Core_models import SystemSetting
from myapp.Services.email_parsers import detect_parser
from myapp.Services.email_matcher import match_email_to_payment

logger = logging.getLogger(__name__)


# ── IMAP connection ───────────────────────────────────────────────────
def _imap_connect():
    cfg = settings.BANK_EMAIL_IMAP
    if not cfg.get("user") or not cfg.get("password"):
        raise RuntimeError(
            "BANK_EMAIL_IMAP.user/password are not configured. "
            "Set BANK_EMAIL_USER and BANK_EMAIL_PASS in the environment."
        )
    m = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    m.login(cfg["user"], cfg["password"])
    m.select(cfg.get("folder", "INBOX"))
    return m


# ── MIME body extraction ─────────────────────────────────────────────
def _get_html_body(msg) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/html" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


def _get_text_body(msg) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/plain":
            charset = msg.get_content_charset() or "utf-8"
            return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


# ── Header parsing ───────────────────────────────────────────────────
def _extract_message_id(msg) -> str:
    mid = msg.get("Message-ID") or ""
    if mid:
        return mid.strip()[:512]
    # Fallback — a hash of the From/Date/Subject triple. Not perfect but
    # avoids crashing on malformed receipts.
    fallback = f"{msg.get('From', '')}|{msg.get('Date', '')}|{msg.get('Subject', '')}"
    import hashlib
    return "sha:" + hashlib.sha1(fallback.encode()).hexdigest()


def _extract_sender_email(msg) -> str:
    return parseaddr(msg.get("From", ""))[1].lower().strip()


def _extract_received_at(msg) -> datetime:
    raw = msg.get("Date")
    if not raw:
        return timezone.now()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except Exception:
        return timezone.now()


def _to_decimal(raw) -> Decimal | None:
    if raw is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(raw))
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", ""))
    except Exception:
        return None


# ── Core: process one raw email ──────────────────────────────────────
def process_email(raw_bytes: bytes) -> BankAlertEmail | None:
    """Parse + store + match one raw email. Returns the new record,
    or None if this email was a duplicate / not a bank receipt."""
    msg = email.message_from_bytes(raw_bytes)
    message_id = _extract_message_id(msg)

    # Idempotency guard — do NOT re-insert.
    if BankAlertEmail.objects.filter(message_id=message_id).exists():
        return None

    html = _get_html_body(msg)
    text = _get_text_body(msg)
    if not html and not text:
        logger.debug("Skipping email %s — no HTML/text body", message_id)
        return None
    # STRICT WHITELIST CHECK: If a whitelist is configured, reject any email
    # that doesn't come from an approved bank domain. This prevents personal
    # emails from being stored in the database.
    sender = _extract_sender_email(msg)
    whitelist = settings.BANK_EMAIL_IMAP.get("sender_whitelist") or []
    
    if whitelist:
        # Check if the sender's domain matches any in our list
        sender_lower = sender.lower()
        if not any(sender_lower.endswith(w.lower()) for w in whitelist):
            logger.debug("Skipping email from %s — sender not in whitelist", sender)
            return None

    soup = BeautifulSoup(html or text, "lxml")
    bank_type, parser = detect_parser(soup)

    # soup = BeautifulSoup(html or text, "lxml")
    # bank_type, parser = detect_parser(soup)

    # # Optionally enforce a sender whitelist for unknown banks so stray
    # # personal emails don't get stored as "unknown".
    # sender = _extract_sender_email(msg)
    # whitelist = settings.BANK_EMAIL_IMAP.get("sender_whitelist") or []
    # if not bank_type and whitelist and not any(sender.endswith(w) for w in whitelist):
    #     logger.debug("Skipping email from %s — no fingerprint, not whitelisted", sender)
    #     return None

    extracted: dict = {}
    parser_error = ""
    if parser is not None:
        try:
            extracted = parser(soup, msg) or {}
        except NotImplementedError as exc:
            parser_error = f"parser stub: {exc}"
        except Exception as exc:
            logger.exception("Parser crashed for bank=%s", bank_type)
            parser_error = f"{type(exc).__name__}: {exc}"

    # FALLBACK: If the specific bank parser didn't find an amount, 
    # try the generic regex parser. This catches Zelle, Amex, and 
    # US Bank templates that change slightly.
    if not extracted.get("amount"):
        from myapp.Services.email_parsers import parse_generic_bank
        try:
            fallback = parse_generic_bank(soup, msg)
            if fallback and fallback.get("amount"):
                extracted = fallback
                parser_error = (parser_error + " | " if parser_error else "") + "Used generic fallback."
        except Exception:
            pass

    record = BankAlertEmail.objects.create(
        message_id=message_id,
        sender_email=sender,
        subject=(msg.get("Subject") or "")[:500],
        received_at=_extract_received_at(msg),
        raw_body_html=html or "",
        raw_body_text=text or "",
        bank_type=bank_type or "other",
        extracted_amount=_to_decimal(extracted.get("amount")),
        extracted_currency=(extracted.get("currency") or "USD").upper()[:10],
        extracted_sender_name=(extracted.get("sender_name") or "")[:200],
        extracted_reference=(extracted.get("transaction_id") or "")[:200],
        extraction_notes=extracted.get("note") or "",
        parser_error=parser_error,
        status=BankAlertStatus.UNMATCHED,
    )

    # If the parser couldn't extract an amount, there's nothing to match on.
    if record.extracted_amount is None:
        record.status = BankAlertStatus.IGNORED
        record.notes = "No amount extracted — cannot auto-match."
        record.save(update_fields=["status", "notes", "updated_at"])
        return record

    # Try to auto-match.
    try:
        match_email_to_payment(record)
    except Exception:
        logger.exception("Matcher crashed for record %s", record.id)

    return record


# ── Public entry point — called by the Celery task ───────────────────
def fetch_and_store_emails(max_per_run: int = None, since_uid: int = None) -> int:
    r"""Connect to IMAP, pull new messages using persistent UID watermark, store, and match.

    Uses persistent UID watermark in SystemSetting (key='bank_email_last_uid')
    so that:
    1. We only fetch messages that arrived since the last run.
    2. We do NOT rely on the \Seen / UNSEEN flag (so human reading in Gmail doesn't skip emails).
    3. We do NOT need to mark emails as read if mark_as_seen is False.
    4. Duplicates are strictly prevented by Message-ID check in process_email.
    """
    cfg = settings.BANK_EMAIL_IMAP
    mark_seen = cfg.get("mark_as_seen", False)

    if max_per_run is None:
        max_per_run = cfg.get("max_per_run", 50)

    try:
        m = _imap_connect()
    except Exception:
        logger.exception("IMAP connect failed")
        return 0

    stored = 0
    watermark_key = "bank_email_last_uid"
    last_uid_val = since_uid or SystemSetting.get(watermark_key)

    try:
        if last_uid_val and str(last_uid_val).isdigit():
            last_uid = int(last_uid_val)
            typ, data = m.uid("search", None, f"UID {last_uid + 1}:*")
            if typ != "OK":
                logger.warning("IMAP UID search returned %s", typ)
                return 0
            raw_uids = (data[0] or b"").split()
            # Ensure strictly greater than last_uid (since 'UID X:*' can include X)
            uids = [u for u in raw_uids if int(u) > last_uid]
            logger.info("Bank-email poll: %d new messages since UID %d", len(uids), last_uid)
        else:
            # Initial run without a watermark: start from October 2026 or latest N
            typ, data = m.uid("search", None, "SINCE", "01-Oct-2026")
            raw_uids = (data[0] or b"").split() if typ == "OK" else []
            if not raw_uids:
                typ, data = m.uid("search", None, "ALL")
                raw_uids = (data[0] or b"").split() if typ == "OK" else []
            uids = raw_uids
            logger.info("Bank-email poll (initial seed): %d messages found", len(uids))

        # Sort UIDs numerically ascending (oldest to newest among the new batch)
        uids.sort(key=lambda x: int(x))

        # Process in batches of max_per_run
        uids_to_process = uids[:max_per_run] if len(uids) > max_per_run else uids
        processed_uids = []

        for uid_bytes in uids_to_process:
            uid_str = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
            try:
                typ, msg_data = m.uid("fetch", uid_str, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                record = process_email(raw)
                if record is not None:
                    stored += 1
                if mark_seen:
                    m.uid("store", uid_str, "+FLAGS", "\\Seen")
                processed_uids.append(int(uid_str))
            except Exception:
                logger.exception("Failed to process IMAP uid=%s", uid_str)
                continue

        # Advance watermark to highest UID processed in this run
        if processed_uids:
            new_highest = max(processed_uids)
            SystemSetting.objects.update_or_create(
                key=watermark_key,
                defaults={
                    "value": str(new_highest),
                    "description": "Last processed IMAP message UID for bank alert emails",
                },
            )
            logger.info("Updated bank_email_last_uid watermark to %d", new_highest)
        elif not last_uid_val and raw_uids:
            highest_seen = max(int(u) for u in raw_uids)
            SystemSetting.objects.update_or_create(
                key=watermark_key,
                defaults={
                    "value": str(highest_seen),
                    "description": "Last processed IMAP message UID for bank alert emails",
                },
            )

        return stored
    finally:
        try:
            m.logout()
        except Exception:
            pass