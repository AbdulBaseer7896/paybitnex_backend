"""
UBL Corporate Bank Statement Reconciliation Service for Paidix.

Reconciles outgoing PKR transfers against UBL corporate bank statement CSV exports.
Supports:
  - Parsing UBL CSV format (debits, IBFT beneficiary parsing, internal transfers)
  - 3-day sliding window with 1-day settlement lag buffer
  - Deterministic multi-pass matching (Golden Match, Internal Match, Discrepancy Flagging, Unmatched)
  - Safe --dry-run (default), --commit, and --undo options
"""

import os
import re
import csv
import glob
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from django.conf import settings

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_UBL_STATEMENTS_DIR = os.environ.get(
    "UBL_STATEMENTS_DIR",
    str(PROJECT_ROOT / "Bank_statments")
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Normalization & Matching Utilities
# ─────────────────────────────────────────────────────────────────────────────

def clean_account_title(name: Any) -> str:
    """Normalize account title / beneficiary name by stripping banking noise,
    prefixes, punctuation, and extra whitespace.
    """
    if not name:
        return ""
    text = str(name).strip().upper()
    if not text or text in ("NAN", "NA", "N/A", "NONE", "NULL", "UNKNOWN", "-"):
        return ""

    # Remove common banking noise tags
    text = re.sub(r'\bIBFT\s+FUND\s+TRANSFER\b', '', text)
    text = re.sub(r'\bTO:\s*', '', text)
    text = re.sub(r'\bBANK:.*$', '', text)  # Strip trailing bank name
    text = re.sub(r'\b\(ASAAN\s+AC\b\)?', '', text)
    text = re.sub(r'\b\(SMC-PRIVATE\)\s+LIM\b', '', text)
    text = re.sub(r'\b\(PVT\)\s+LTD\b', '', text)
    text = re.sub(r'\bPVT\s+LTD\b', '', text)
    text = re.sub(r'\bLIMITED\b', '', text)
    text = re.sub(r'\bVIA\s+RAAST\b', '', text)

    # Strip leading/trailing dashes, underscores, dots, slashes
    text = re.sub(r'^[\s\-_./]+|[\s\-_./]+$', '', text)

    # Replace non-alphanumeric with space
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def names_match(claimed_name: str, bank_name: str) -> Tuple[bool, float, str]:
    """Compare customer bank account holder name with statement beneficiary name.
    Returns:
        (is_match: bool, score: float, reason: str)
    """
    c_clean = clean_account_title(claimed_name)
    b_clean = clean_account_title(bank_name)

    if not b_clean:
        return False, 0.0, "Statement beneficiary name is omitted"
    if not c_clean:
        return False, 0.0, "Customer account holder name not provided"

    # 1. Exact normalized match
    if c_clean == b_clean:
        return True, 1.0, "Exact match"

    # 2. Token subset match (e.g. 'HASSNAIN YASIN' in 'HASSNAIN YASIN KHAN' or vice versa)
    c_tokens = set(c_clean.split())
    b_tokens = set(b_clean.split())

    if c_tokens and b_tokens:
        intersection = c_tokens.intersection(b_tokens)
        min_len = min(len(c_tokens), len(b_tokens))
        if len(intersection) >= min_len and min_len >= 2:
            score = len(intersection) / max(len(c_tokens), len(b_tokens))
            return True, score, f"Token match ({len(intersection)} shared names)"

    return False, 0.0, f"Discrepancy (System: '{c_clean}' vs Bank: '{b_clean}')"


def amounts_match(system_amt: Decimal, bank_amt: Decimal) -> Tuple[bool, str]:
    """Checks if system PKR amount matches bank statement debit.
    In Pakistan (1Link/IBFT/Raast), decimals/paisas cannot be transferred.
    When sending funds, accountants drop decimals (e.g. 146,308.80 -> 146,308)
    or round to the nearest whole rupee (146,309).
    """
    if system_amt == bank_amt:
        return True, "exact"
    # Dropped decimals (truncated integer)
    if Decimal(int(system_amt)) == bank_amt:
        return True, "truncated_decimals"
    # Rounded to nearest whole rupee
    if Decimal(round(system_amt)) == bank_amt:
        return True, "rounded_rupee"
    # Integer match within 1 rupee
    if int(system_amt) == int(bank_amt) and abs(system_amt - bank_amt) < Decimal("1.00"):
        return True, "integer_match"
    return False, "mismatch"


# ─────────────────────────────────────────────────────────────────────────────
# 2. UBL Statement Parser
# ─────────────────────────────────────────────────────────────────────────────

def get_latest_statement_file(folder_path: Optional[str] = None) -> Optional[str]:
    """Finds the most recently created/modified UBL statement CSV file across standard candidate directories."""
    candidate_dirs = []
    if folder_path:
        candidate_dirs.append(folder_path)
    if hasattr(settings, "UBL_STATEMENTS_DIR") and getattr(settings, "UBL_STATEMENTS_DIR"):
        candidate_dirs.append(getattr(settings, "UBL_STATEMENTS_DIR"))
    candidate_dirs.append(DEFAULT_UBL_STATEMENTS_DIR)
    candidate_dirs.append(str(PROJECT_ROOT / "Bank_statments"))
    candidate_dirs.append(r"C:\Users\Abdullah Shahid\Downloads\UBL_scrapper\Bank_statments")

    for search_dir in candidate_dirs:
        if not search_dir or not os.path.exists(search_dir):
            continue
        files = glob.glob(os.path.join(search_dir, "UBL_Statement_*.csv"))
        if not files:
            files = glob.glob(os.path.join(search_dir, "*.csv"))
        if files:
            files.sort(key=os.path.getmtime, reverse=True)
            return files[0]
    return None


def parse_ubl_statement(file_path: str) -> List[Dict[str, Any]]:
    """Reads a UBL Corporate CSV export, extracts all Debits ('D'), and normalizes
    amounts, dates, beneficiary titles, and bank references.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Statement file not found: {file_path}")

    records = []
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [name.strip() if name else "" for name in (reader.fieldnames or [])]

        cr_dr_col = "CrDr Ind"
        if cr_dr_col not in reader.fieldnames:
            raise KeyError(f"Expected column '{cr_dr_col}' not found in UBL statement.")

        for line_no, raw_row in enumerate(reader, start=2):
            cr_dr = str(raw_row.get(cr_dr_col, "")).strip().upper()
            # Outgoing transfers are DEBITS ('D')
            if cr_dr != "D":
                continue

            row = {k.strip(): (v.strip() if v else "") for k, v in raw_row.items() if k}

            # Parse amount (Tx Amount: e.g. "41,081.00")
            raw_amt = row.get("Tx Amount", "").replace(",", "").strip()
            if not raw_amt:
                continue
            try:
                clean_amt = Decimal(raw_amt).quantize(Decimal("0.01"))
            except InvalidOperation:
                continue

            # Parse transaction date (Tx Date: DD/MM/YYYY)
            raw_date = row.get("Tx Date") or row.get("Tx Post Date")
            parsed_date = None
            if raw_date:
                for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                    try:
                        parsed_date = datetime.strptime(raw_date, fmt).date()
                        break
                    except ValueError:
                        pass

            desc = row.get("Tx Desc") or row.get("Tx Desc2") or ""
            tran_type = row.get("Tran Type", "").strip()

            # Extract Beneficiary Name & Bank from description
            beneficiary_name = ""
            beneficiary_bank = ""
            internal_target_acct = ""
            ref_no = row.get("Tx Ref No") or row.get("Channel Ref No") or ""

            # Pattern 1: IBFT
            ibft_match = re.search(r'TO:\s*(.+?)(?:\s+BANK:\s*(.+)|$)', desc, re.IGNORECASE)
            if ibft_match:
                beneficiary_name = ibft_match.group(1).strip()
                if ibft_match.group(2):
                    beneficiary_bank = ibft_match.group(2).strip()

            # Pattern 2: Internal UBL transfer (0832)
            internal_match = re.search(r'TO-(\d+)', desc)
            if internal_match:
                internal_target_acct = internal_match.group(1).strip()

            # Pattern 3: Ref number in description
            ref_match = re.search(r'REF\s*NO\.?\s*(\d+)', desc, re.IGNORECASE)
            if ref_match:
                ref_no = ref_match.group(1).strip()

            records.append({
                "LINE_NO": line_no,
                "ACCOUNT_NO": row.get("Account No", ""),
                "TRAN_TYPE": tran_type,
                "CR_DR": cr_dr,
                "RAW_AMOUNT": raw_amt,
                "CLEAN_AMT": clean_amt,
                "RAW_DATE": raw_date,
                "PARSED_DATE": parsed_date,
                "DESC": desc,
                "BENEFICIARY_NAME": beneficiary_name,
                "CLEAN_BENEFICIARY": clean_account_title(beneficiary_name),
                "BENEFICIARY_BANK": beneficiary_bank,
                "INTERNAL_TARGET_ACCT": internal_target_acct,
                "REF_NO": ref_no,
                "RAW_ROW": row,
            })

    return records


# ─────────────────────────────────────────────────────────────────────────────
# 3. Date Window Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_reconciliation_window(
    reference_date: Optional[date] = None,
    window_days: int = 3,
    lag_days: int = 1,
) -> Tuple[date, date]:
    """Calculates the 3-day reconciliation window with a 1-day lag.
    Example: if reference_date is Sept 11:
      - lag_days=1 -> end_date = Sept 10
      - window_days=3 -> start_date = Sept 8 (Sept 8, 9, 10)
    """
    ref = reference_date or date.today()
    end_date = ref - timedelta(days=lag_days)
    start_date = end_date - timedelta(days=window_days - 1)
    return start_date, end_date


# ─────────────────────────────────────────────────────────────────────────────
# 4. Reconciliation Engine
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_ubl_transfers(
    file_path: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    dry_run: bool = True,
    source: str = "manual_cli",
    executed_by=None,
    undo: bool = False,
    only_unverified: bool = True,
) -> Dict[str, Any]:
    """Reconciles OutgoingPKRTransfer records against UBL bank statement debits.

    Args:
        file_path: Path to UBL statement CSV (defaults to latest in Bank_statments folder)
        start_date: Start of transfer window (inclusive)
        end_date: End of transfer window (inclusive)
        dry_run: If True, simulates without modifying the database
        source: 'manual_cli', 'cron', etc.
        executed_by: User instance or None
        undo: If True, rolls back verified transfers in the window back to unverified
        only_unverified: If True, skips transfers that are already verified and protects them
    """
    from myapp.Models.Transaction_models import OutgoingPKRTransfer

    # Resolve date window
    if not start_date or not end_date:
        calc_start, calc_end = get_reconciliation_window()
        start_date = start_date or calc_start
        end_date = end_date or calc_end

    # ── UNDO MODE ────────────────────────────────────────────────────────────
    if undo:
        qs_to_undo = OutgoingPKRTransfer.objects.filter(
            transfer_date__gte=start_date,
            transfer_date__lte=end_date,
            bank_verification_source__in=["cron", "manual_cli"],
        )
        count = qs_to_undo.count()
        if not dry_run:
            from myapp.Models.Audit_models import AuditLog
            with transaction.atomic():
                for t in list(qs_to_undo):
                    old_status = t.bank_verification_status
                    old_verified = t.bank_verified
                    old_notes = t.bank_verification_notes
                    t.bank_verified = False
                    t.bank_verification_status = "unverified"
                    t.bank_verified_at = None
                    t.bank_verification_notes = ""
                    t.bank_statement_ref = ""
                    t.bank_statement_matched_data = None
                    t.save(update_fields=[
                        "bank_verified", "bank_verification_status", "bank_verified_at",
                        "bank_verification_notes", "bank_statement_ref", "bank_statement_matched_data",
                    ])
                    AuditLog.record(
                        user=executed_by,
                        action=AuditLog.ACTION_UPDATE,
                        target=t,
                        target_label=t.reference,
                        description=f"{t.reference}: Bank statement verification undone / reverted to unverified.",
                        before={"bank_verification_status": old_status, "bank_verified": old_verified, "notes": old_notes},
                        after={"bank_verification_status": "unverified", "bank_verified": False, "notes": ""},
                        metadata={"engine": "ubl_reconciliation", "action": "undo"},
                    )
        return {
            "mode": "undo",
            "dry_run": dry_run,
            "reverted_count": count,
            "message": f"{'[DRY RUN] Would revert' if dry_run else 'Successfully reverted'} {count} transfer(s) to unverified status for {start_date} to {end_date}.",
        }

    # ── STATEMENT FILE RESOLUTION ────────────────────────────────────────────
    resolved_file = file_path or get_latest_statement_file()
    if not resolved_file:
        raise FileNotFoundError(
            f"No UBL statement CSV file found. Please specify --file or ensure files exist in {DEFAULT_UBL_STATEMENTS_DIR}"
        )

    bank_records = parse_ubl_statement(resolved_file)

    # ── QUERY OUTGOING TRANSFERS ─────────────────────────────────────────────
    # Target outgoing transfers in the window.
    base_qs = OutgoingPKRTransfer.objects.filter(
        transfer_date__gte=start_date,
        transfer_date__lte=end_date,
    )

    total_in_window = base_qs.count()
    skipped_verified = 0
    skipped_flagged = 0
    used_bank_indices = set()

    if only_unverified:
        # Strictly only check transfers that have NOT been touched yet (status == 'unverified').
        # Any transaction that has been approved ('verified') or flagged ('discrepancy') is untouched.
        already_processed_qs = base_qs.exclude(bank_verification_status="unverified")
        skipped_verified = already_processed_qs.filter(bank_verification_status="verified").count()
        skipped_flagged = already_processed_qs.filter(bank_verification_status="discrepancy").count()

        # Collect statement refs already used by touched transfers so they cannot be reused
        used_refs = set(
            already_processed_qs.exclude(bank_statement_ref="")
            .values_list("bank_statement_ref", flat=True)
        )
        for idx, bank_row in enumerate(bank_records):
            if bank_row.get("REF_NO") and bank_row["REF_NO"] in used_refs:
                used_bank_indices.add(idx)

        transfers_qs = base_qs.filter(bank_verification_status="unverified")
    else:
        transfers_qs = base_qs

    transfers_qs = (
        transfers_qs
        .select_related(
            "customer_bank_account",
            "customer_bank_account__bank",
            "customer_bank_account__customer",
            "sent_by",
            "incoming_payment",
        )
        .prefetch_related("payments")
    )
    transfers = list(transfers_qs)

    matched_transfers: Dict[int, Dict[str, Any]] = {}

    # ── PASS 1: Exact Date + Golden Match (Amount + Name Match) ──────────────
    for t in transfers:
        if t.id in matched_transfers:
            continue
        c_amt = t.amount_pkr
        t_date = t.transfer_date
        c_name = t.customer_bank_account.holder_name if t.customer_bank_account else ""

        for idx, bank_row in enumerate(bank_records):
            if idx in used_bank_indices:
                continue
            amt_ok, _ = amounts_match(c_amt, bank_row["CLEAN_AMT"])
            if not amt_ok:
                continue
            if bank_row["PARSED_DATE"] == t_date and bank_row["CLEAN_BENEFICIARY"]:
                is_match, score, reason = names_match(c_name, bank_row["BENEFICIARY_NAME"])
                if is_match:
                    matched_transfers[t.id] = {
                        "transfer": t,
                        "status": "verified",
                        "bank_record": bank_row,
                        "match_type": "golden_match",
                        "score": score,
                        "notes": f"Golden Match: Amount PKR {c_amt:,.2f} (Bank: PKR {bank_row['CLEAN_AMT']:,.2f}) on {t_date} matches statement beneficiary '{bank_row['BENEFICIARY_NAME']}'.",
                    }
                    used_bank_indices.add(idx)
                    break

    # ── PASS 2: Interbank Lag (+-1 Day) + Golden Match ───────────────────────
    for t in transfers:
        if t.id in matched_transfers:
            continue
        c_amt = t.amount_pkr
        t_date = t.transfer_date
        c_name = t.customer_bank_account.holder_name if t.customer_bank_account else ""

        for idx, bank_row in enumerate(bank_records):
            if idx in used_bank_indices:
                continue
            amt_ok, _ = amounts_match(c_amt, bank_row["CLEAN_AMT"])
            if not amt_ok:
                continue
            b_date = bank_row["PARSED_DATE"]
            if b_date and abs((b_date - t_date).days) <= 1 and bank_row["CLEAN_BENEFICIARY"]:
                is_match, score, reason = names_match(c_name, bank_row["BENEFICIARY_NAME"])
                if is_match:
                    matched_transfers[t.id] = {
                        "transfer": t,
                        "status": "verified",
                        "bank_record": bank_row,
                        "match_type": "golden_match_adjacent_date",
                        "score": score,
                        "notes": f"Golden Match (Date buffer {b_date} vs {t_date}): Amount PKR {c_amt:,.2f} (Bank: PKR {bank_row['CLEAN_AMT']:,.2f}) matches statement beneficiary '{bank_row['BENEFICIARY_NAME']}'.",
                    }
                    used_bank_indices.add(idx)
                    break

    # ── PASS 3: Internal Transfer Match (Tran Type 0832) ─────────────────────
    for t in transfers:
        if t.id in matched_transfers:
            continue
        c_amt = t.amount_pkr
        t_date = t.transfer_date
        acct_no = t.customer_bank_account.account_number if t.customer_bank_account else ""

        for idx, bank_row in enumerate(bank_records):
            if idx in used_bank_indices:
                continue
            amt_ok, _ = amounts_match(c_amt, bank_row["CLEAN_AMT"])
            if not amt_ok:
                continue
            b_date = bank_row["PARSED_DATE"]
            if b_date and abs((b_date - t_date).days) <= 1:
                target_acct = bank_row.get("INTERNAL_TARGET_ACCT", "")
                if target_acct and acct_no and target_acct in acct_no:
                    matched_transfers[t.id] = {
                        "transfer": t,
                        "status": "verified",
                        "bank_record": bank_row,
                        "match_type": "internal_account_match",
                        "score": 1.0,
                        "notes": f"Internal Transfer Match: Debit of PKR {bank_row['CLEAN_AMT']:,.2f} on {b_date} sent to UBL account {target_acct}.",
                    }
                    used_bank_indices.add(idx)
                    break

    # ── PASS 4: Amount & Date Match, Name Discrepancy (Flagged) ──────────────
    for t in transfers:
        if t.id in matched_transfers:
            continue
        c_amt = t.amount_pkr
        t_date = t.transfer_date
        c_name = t.customer_bank_account.holder_name if t.customer_bank_account else ""

        for idx, bank_row in enumerate(bank_records):
            if idx in used_bank_indices:
                continue
            amt_ok, _ = amounts_match(c_amt, bank_row["CLEAN_AMT"])
            if not amt_ok:
                continue
            b_date = bank_row["PARSED_DATE"]
            if b_date and abs((b_date - t_date).days) <= 1:
                bank_title = bank_row["BENEFICIARY_NAME"] or "Name Omitted"
                matched_transfers[t.id] = {
                    "transfer": t,
                    "status": "discrepancy",
                    "bank_record": bank_row,
                    "match_type": "name_discrepancy",
                    "score": 0.0,
                    "notes": f"Debit of PKR {bank_row['CLEAN_AMT']:,.2f} (System: PKR {c_amt:,.2f}) found on {b_date}, but title differs (System: '{c_name}' vs Bank: '{bank_title}'). Requires admin review.",
                }
                used_bank_indices.add(idx)
                break

    # ── PASS 5: Unmatched (In System, Not in Bank Statement) ─────────────────
    unmatched_list = []
    for t in transfers:
        if t.id not in matched_transfers:
            matched_transfers[t.id] = {
                "transfer": t,
                "status": "unmatched",
                "bank_record": None,
                "match_type": "unmatched",
                "score": 0.0,
                "notes": f"Unmatched: Transfer of PKR {t.amount_pkr:,.2f} on {t.transfer_date} has no matching debit in the UBL statement window.",
            }
            unmatched_list.append(t)

    # ── DATABASE COMMIT (IF NOT DRY-RUN) ─────────────────────────────────────
    golden_matches = [m for m in matched_transfers.values() if m["status"] == "verified"]
    discrepancies = [m for m in matched_transfers.values() if m["status"] == "discrepancy"]
    unmatched = [m for m in matched_transfers.values() if m["status"] == "unmatched"]

    now = timezone.now()
    if not dry_run:
        from myapp.Models.Audit_models import AuditLog

        with transaction.atomic():
            for m in matched_transfers.values():
                t = m["transfer"]
                b_rec = m.get("bank_record")
                decision_status = m["status"]

                old_status = t.bank_verification_status
                old_verified = t.bank_verified
                old_source = t.bank_verification_source
                old_notes = t.bank_verification_notes or ""

                t.bank_verified = (decision_status == "verified")
                t.bank_verification_status = decision_status
                t.bank_verification_source = source
                t.bank_verified_at = now if decision_status == "verified" else None
                t.bank_verification_notes = m["notes"]
                t.bank_statement_ref = (b_rec.get("REF_NO") if b_rec else "") or ""
                t.bank_statement_matched_data = b_rec.get("RAW_ROW") if b_rec else None

                t.save(update_fields=[
                    "bank_verified",
                    "bank_verification_status",
                    "bank_verification_source",
                    "bank_verified_at",
                    "bank_verification_notes",
                    "bank_statement_ref",
                    "bank_statement_matched_data",
                ])

                # Construct descriptive explanation for audit log
                if decision_status == "verified":
                    desc_action = f"Bank statement verification: Automatically verified against statement ref '{t.bank_statement_ref}' (Match: {m.get('match_type')}, Score: {m.get('score', 1.0):.2f})."
                elif decision_status == "discrepancy":
                    desc_action = f"Bank statement verification: Flagged discrepancy for admin review. {m.get('notes')}"
                else:
                    desc_action = f"Bank statement verification: Marked unmatched. {m.get('notes')}"

                # 1. Audit log on OutgoingPKRTransfer
                AuditLog.record(
                    user=None,
                    action=AuditLog.ACTION_UPDATE,
                    target=t,
                    target_label=t.reference,
                    description=f"{t.reference}: {desc_action}",
                    before={
                        "bank_verification_status": old_status,
                        "bank_verified": old_verified,
                        "bank_verification_source": old_source,
                        "bank_verification_notes": old_notes,
                    },
                    after={
                        "bank_verification_status": decision_status,
                        "bank_verified": t.bank_verified,
                        "bank_verification_source": source,
                        "bank_verified_at": t.bank_verified_at.isoformat() if t.bank_verified_at else None,
                        "bank_verification_notes": t.bank_verification_notes,
                        "bank_statement_ref": t.bank_statement_ref,
                    },
                    metadata={
                        "engine": "ubl_reconciliation",
                        "source": source,
                        "match_type": m.get("match_type"),
                        "match_score": m.get("score"),
                        "decision": decision_status,
                        "transfer_id": str(t.id),
                        "transfer_reference": t.reference,
                        "statement_ref": t.bank_statement_ref,
                    },
                )

                # 2. Audit log on linked IncomingPayment(s)
                linked_payments = []
                if getattr(t, "incoming_payment", None):
                    linked_payments.append(t.incoming_payment)
                for p in t.payments.all():
                    if p not in linked_payments:
                        linked_payments.append(p)

                for p in linked_payments:
                    AuditLog.record(
                        user=None,
                        action=AuditLog.ACTION_UPDATE,
                        target=p,
                        target_label=p.reference,
                        description=f"{p.reference} (Transfer {t.reference}): {desc_action}",
                        before={
                            "bank_verification_status": old_status,
                            "bank_verified": old_verified,
                            "bank_verification_source": old_source,
                            "bank_verification_notes": old_notes,
                        },
                        after={
                            "bank_verification_status": decision_status,
                            "bank_verified": t.bank_verified,
                            "bank_verification_source": source,
                            "bank_verified_at": t.bank_verified_at.isoformat() if t.bank_verified_at else None,
                            "bank_verification_notes": t.bank_verification_notes,
                            "bank_statement_ref": t.bank_statement_ref,
                        },
                        metadata={
                            "engine": "ubl_reconciliation",
                            "source": source,
                            "match_type": m.get("match_type"),
                            "match_score": m.get("score"),
                            "decision": decision_status,
                            "payment_id": str(p.id),
                            "payment_reference": p.reference,
                            "transfer_id": str(t.id),
                            "transfer_reference": t.reference,
                            "statement_ref": t.bank_statement_ref,
                        },
                    )

    return {
        "mode": "reconcile",
        "dry_run": dry_run,
        "statement_file": os.path.basename(resolved_file),
        "statement_path": resolved_file,
        "start_date": start_date,
        "end_date": end_date,
        "total_in_window": total_in_window,
        "unverified_transfers_count": len(transfers),
        "total_transfers": len(transfers),
        "skipped_verified": skipped_verified,
        "skipped_flagged": skipped_flagged,
        "total_bank_debits": len(bank_records),
        "golden_matches_count": len(golden_matches),
        "discrepancies_count": len(discrepancies),
        "unmatched_count": len(unmatched),
        "golden_matches": golden_matches,
        "discrepancies": discrepancies,
        "unmatched": unmatched,
    }
