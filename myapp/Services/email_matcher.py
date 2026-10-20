"""
email_matcher.py — Link a BankAlertEmail to the correct IncomingPayment.

Called automatically when a new email is stored, and can be called
manually from the Conflicts tab to resolve an ambiguous match.
"""
from __future__ import annotations
import logging
from datetime import timedelta
from decimal import Decimal
from django.db import transaction as dbtx
from django.utils import timezone

from myapp.Models.BankEmail_models import BankAlertEmail, BankAlertStatus
from myapp.Models.Transaction_models import (
    IncomingPayment, TransactionStatus, TransactionStatusHistory,
)

logger = logging.getLogger(__name__)

# How many calendar days to look either side of the email's received_at.
DATE_WINDOW_DAYS = 2
# Small tolerance for currency rounding (bank fees etc.) — set to 0 if you
# want strict amount equality.
AMOUNT_TOLERANCE = Decimal("0.01")


def _normalise_amount(raw) -> Decimal | None:
    """'$169.00' / '+$169.00' / '1,500.00 USD' / 'USD 169' → Decimal."""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    s = str(raw)
    # First number-like substring wins.
    m = __import__("re").search(r"[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", ""))
    except Exception:
        return None


def _normalise_name(name: str) -> str:
    return "".join(c.lower() for c in (name or "") if c.isalnum() or c.isspace()).strip()


def _find_candidates(email_rec: BankAlertEmail):
    """Return a list of pending payments that could match this email."""
    # Only SUBMITTED / UNDER_REVIEW payments are candidates — once
    # verified, the payment has already moved past this stage.
    qs = IncomingPayment.objects.filter(
        status__in=[TransactionStatus.SUBMITTED, TransactionStatus.UNDER_REVIEW],
    ).select_related("customer")

    # 1) Exact reference match — the strongest signal.
    ref = (email_rec.extracted_reference or "").strip()
    if ref:
        ref_hits = qs.filter(external_transaction_id__iexact=ref)
        if ref_hits.exists():
            return list(ref_hits)

    # 2) Amount + date window (+ optional sender-name tiebreaker).
    amount = _normalise_amount(email_rec.extracted_amount)
    if amount is None:
        return []

    lo = (email_rec.received_at - timedelta(days=DATE_WINDOW_DAYS)).date()
    hi = (email_rec.received_at + timedelta(days=DATE_WINDOW_DAYS)).date()

    # Amount filter with tolerance: BETWEEN amount−tol AND amount+tol
    from django.db.models import Q
    
    amt_hits = list(
        qs.filter(
            currency__code__iexact=email_rec.extracted_currency or "USD",
            amount__gte=amount - AMOUNT_TOLERANCE,
            amount__lte=amount + AMOUNT_TOLERANCE,
        ).filter(
            # Match if occurred_on is within range OR (if occurred_on is null) created_at is within range
            Q(occurred_on__range=(lo, hi)) | 
            Q(occurred_on__isnull=True, created_at__date__range=(lo, hi))
        )
    )

    if len(amt_hits) <= 1:
        return amt_hits

    # 3) Sender-name tiebreaker — only kicks in when >1 amount match.
    email_name = _normalise_name(email_rec.extracted_sender_name)
    if not email_name:
        return amt_hits

    scored = []
    for p in amt_hits:
        p_name = _normalise_name(p.sender_name or "")
        if not p_name:
            continue
        # Substring either way — "John Doe" vs "JOHN DOE" both match.
        if email_name in p_name or p_name in email_name:
            scored.append(p)
    return scored or amt_hits


def _auto_match(email_rec: BankAlertEmail, payment: IncomingPayment):
    """Mark the payment auto-verified and link it to the email."""
    before = payment.status
    with dbtx.atomic():
        payment.verified_at = timezone.now()
        payment.verified_by = None  # system, not a user
        payment.verified_note = (
            f"Auto-verified by bank email "
            f"({email_rec.bank_type or 'unknown'}"
            f"{', ref ' + email_rec.extracted_reference if email_rec.extracted_reference else ''})"
        )
        payment.auto_verified = True
        payment.linked_bank_email = email_rec
        if payment.status == TransactionStatus.SUBMITTED:
            payment.status = TransactionStatus.UNDER_REVIEW
        payment.save(update_fields=[
            "verified_at", "verified_by", "verified_note",
            "auto_verified", "linked_bank_email", "status", "updated_at",
        ])

        if before != payment.status:
            TransactionStatusHistory.objects.create(
                payment=payment, from_status=before, to_status=payment.status,
                changed_by=None,
                note=f"Auto-verified by bank email {email_rec.id}",
            )

        email_rec.status = BankAlertStatus.AUTO_MATCHED
        email_rec.matched_payment = payment
        email_rec.matched_at = timezone.now()
        email_rec.save(update_fields=[
            "status", "matched_payment", "matched_at", "updated_at",
        ])


def _mark_conflict(email_rec: BankAlertEmail, candidates):
    email_rec.status = BankAlertStatus.CONFLICT
    email_rec.conflict_payment_ids = [str(c.id) for c in candidates]
    email_rec.save(update_fields=["status", "conflict_payment_ids", "updated_at"])


def match_email_to_payment(email_rec: BankAlertEmail):
    """Entry point. Called after every BankAlertEmail is created."""
    try:
        candidates = _find_candidates(email_rec)
    except Exception:
        logger.exception("Matcher failed for email %s", email_rec.id)
        email_rec.status = BankAlertStatus.UNMATCHED
        email_rec.notes = (email_rec.notes + "\nMatcher error.").strip()
        email_rec.save(update_fields=["status", "notes", "updated_at"])
        return

    if len(candidates) == 1:
        _auto_match(email_rec, candidates[0])
    elif len(candidates) > 1:
        _mark_conflict(email_rec, candidates)
    else:
        email_rec.status = BankAlertStatus.UNMATCHED
        email_rec.save(update_fields=["status", "updated_at"])



def match_payment_to_unmatched_emails(payment):
    """
    When a new payment is created, check all existing UNMATCHED emails
    to see if any of them match this payment.
    """
    from myapp.Models.BankEmail_models import BankAlertEmail, BankAlertStatus
    
    # Only try to match payments that are freshly submitted
    if payment.status not in (TransactionStatus.SUBMITTED, TransactionStatus.UNDER_REVIEW):
        return

    # Get all currently unmatched emails
    unmatched_emails = BankAlertEmail.objects.filter(status=BankAlertStatus.UNMATCHED)
    
    for email_rec in unmatched_emails:
        try:
            candidates = _find_candidates(email_rec)
        except Exception:
            continue
            
        # If this email's only candidate is the new payment, auto-match them!
        if len(candidates) == 1 and candidates[0].pk == payment.pk:
            _auto_match(email_rec, payment)
            break 
        # If the new payment is one of several candidates, flag as conflict
        elif len(candidates) > 1 and any(c.pk == payment.pk for c in candidates):
            _mark_conflict(email_rec, candidates)