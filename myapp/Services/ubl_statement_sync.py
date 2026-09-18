"""
UBL Statement Ingestion Service for Paidix.

Parses downloaded UBL Bank Statement CSV files and persists every record into
BankStatementRecord with deterministic SHA-256 fingerprinting for zero duplication.
"""

import os
import csv
import hashlib
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
from typing import Dict, List, Any, Optional, Set

from django.utils import timezone
from myapp.Models.BankAudit_models import BankStatementRecord, BankSyncJob

log = logging.getLogger(__name__)


def generate_transaction_hash(
    account_no: str,
    tran_ref: str,
    channel_ref: str,
    tran_date_str: str,
    amount_str: str,
    cr_dr: str,
    running_bal_str: str = "",
    tran_desc: str = ""
) -> str:
    """Generates a deterministic SHA-256 fingerprint for a bank transaction."""
    raw_key = (
        f"{str(account_no).strip()}|"
        f"{str(tran_ref).strip()}|"
        f"{str(channel_ref).strip()}|"
        f"{str(tran_date_str).strip()}|"
        f"{str(amount_str).replace(',', '').strip()}|"
        f"{str(cr_dr).strip().upper()}|"
        f"{str(running_bal_str).replace(',', '').strip()}|"
        f"{str(tran_desc).strip()[:80]}"
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def clean_amount(val: Any) -> Decimal:
    """Converts a raw amount string into a clean Decimal."""
    if val is None:
        return Decimal("0.00")
    clean_str = str(val).replace(",", "").strip()
    try:
        return Decimal(clean_str)
    except InvalidOperation:
        return Decimal("0.00")


def parse_flexible_date(val: Optional[str]) -> Optional[date]:
    """Parses various date/datetime string formats common in Pakistani bank statements."""
    if not val:
        return None
    val_clean = str(val).strip()
    if not val_clean or val_clean.upper() in ("NA", "NULL", "NONE", "-"):
        return None

    formats = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %I:%M:%S %p",
        "%d-%m-%Y %I:%M:%S %p",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(val_clean, fmt)
            return dt.date()
        except ValueError:
            pass
    return None


def get_field_val(row: Dict[str, Any], candidate_keys: List[str], default: str = "") -> str:
    """Searches case-insensitively across possible column name variations."""
    row_lower = {k.strip().lower(): v for k, v in row.items() if k}
    for candidate in candidate_keys:
        cand_lower = candidate.strip().lower()
        if cand_lower in row_lower:
            val = row_lower[cand_lower]
            return str(val).strip() if val is not None else default
    return default


def ingest_ubl_statement_file(file_path: str, sync_job: Optional[BankSyncJob] = None) -> Dict[str, Any]:
    """Parses a UBL statement CSV file, extracts all transactions (Credits & Debits),
    and persists records into BankStatementRecord.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Statement file not found: {file_path}")

    records_to_create: List[BankStatementRecord] = []
    seen_hashes_in_batch: Set[str] = set()

    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row_raw in reader:
            if not row_raw:
                continue

            # Identify Credit or Debit
            cr_dr_val = get_field_val(row_raw, ["CrDr Ind", "CR_DR_MAINT_IND", "CrDr", "Type", "CR/DR", "DR/CR"])
            cr_dr_clean = cr_dr_val.upper()
            if "CR" in cr_dr_clean or cr_dr_clean == "C":
                cr_dr = "C"
            elif "DR" in cr_dr_clean or cr_dr_clean == "D":
                cr_dr = "D"
            else:
                cr_dr = cr_dr_clean[:10]

            amt_val = get_field_val(row_raw, ["Tx Amount", "TRAN_AMT", "Amount", "TX_AMT"])
            amt = clean_amount(amt_val)
            if amt <= Decimal("0.00"):
                continue

            account_no = get_field_val(row_raw, ["Account No", "ACCT_NO", "Account Number", "Account"])
            channel_ref = get_field_val(row_raw, ["Channel Ref", "Channel Re", "CHANNEL_REF", "Channel"])
            tran_type = get_field_val(row_raw, ["Tran Type", "TRAN_TYPE", "Transaction Type"])
            tx_desc = get_field_val(row_raw, ["Tx Desc", "TRAN_DESC", "Description", "Tx Desc1"])
            tx_desc2 = get_field_val(row_raw, ["Tx Desc2", "TRAN_DESC2", "Desc 2"])
            tx_desc3 = get_field_val(row_raw, ["Tx Desc3", "TRAN_DESC3", "Desc 3"])
            tx_desc4 = get_field_val(row_raw, ["Tx Desc4", "TRAN_DESC4", "Desc 4"])
            tran_ref = get_field_val(row_raw, ["Tx Ref No", "TRAN_REF", "Reference", "Ref No", "SEQ_NO"])

            ccy = get_field_val(row_raw, ["Tx Ccy", "CCY", "Currency"], default="PKR") or "PKR"
            equiv_amt_val = get_field_val(row_raw, ["Tx Equiv Am", "Tx Equiv Amount", "EQUIV_AMT"])
            equiv_amt = clean_amount(equiv_amt_val) if equiv_amt_val else None
            equiv_ccy = get_field_val(row_raw, ["Tx Equiv Cc", "Tx Equiv Currency", "EQUIV_CCY"])

            running_bal_val = get_field_val(row_raw, ["Tx Running", "Tx Running Balance", "RUNNING_BAL", "ACTUAL_BALANCE"])
            running_bal = clean_amount(running_bal_val) if running_bal_val else None
            running_bal_ccy = get_field_val(row_raw, ["Tx Running Ccy", "RUNNING_CCY"])

            raw_tx_date = get_field_val(row_raw, ["Tx Date", "TRAN_DATE", "Date", "TIME_STAMP"])
            raw_post_date = get_field_val(row_raw, ["Tx Post Dat", "Tx Post Date", "POST_DATE"])

            parsed_tx_date = parse_flexible_date(raw_tx_date)
            parsed_post_date = parse_flexible_date(raw_post_date)

            unique_hash = generate_transaction_hash(
                account_no=account_no,
                tran_ref=tran_ref,
                channel_ref=channel_ref,
                tran_date_str=raw_tx_date or (str(parsed_tx_date) if parsed_tx_date else ""),
                amount_str=str(amt),
                cr_dr=cr_dr,
                running_bal_str=str(running_bal) if running_bal is not None else "",
                tran_desc=tx_desc
            )

            if unique_hash in seen_hashes_in_batch:
                continue
            seen_hashes_in_batch.add(unique_hash)

            record = BankStatementRecord(
                account_number=account_no,
                channel_ref=channel_ref,
                cr_dr=cr_dr,
                tran_type=tran_type,
                amount=amt,
                currency=ccy,
                equiv_amount=equiv_amt,
                equiv_currency=equiv_ccy,
                running_balance=running_bal,
                running_balance_currency=running_bal_ccy,
                tran_date=parsed_tx_date,
                post_date=parsed_post_date,
                tran_ref=tran_ref,
                tran_desc=tx_desc,
                tran_desc2=tx_desc2,
                tran_desc3=tx_desc3,
                tran_desc4=tx_desc4,
                raw_data=row_raw,
                unique_hash=unique_hash,
            )
            records_to_create.append(record)

    total_in_file = len(records_to_create)
    if not records_to_create:
        return {
            "total_in_file": 0,
            "newly_inserted": 0,
            "skipped_duplicates": 0,
            "credits_inserted": 0,
            "debits_inserted": 0,
        }

    # Query database for existing hashes
    batch_hashes = [r.unique_hash for r in records_to_create]
    existing_hashes = set(
        BankStatementRecord.objects.filter(unique_hash__in=batch_hashes).values_list("unique_hash", flat=True)
    )

    truly_new_records = [r for r in records_to_create if r.unique_hash not in existing_hashes]
    credits_inserted = sum(1 for r in truly_new_records if r.cr_dr in ("C", "CR"))
    debits_inserted = sum(1 for r in truly_new_records if r.cr_dr in ("D", "DR"))
    skipped_count = total_in_file - len(truly_new_records)

    if truly_new_records:
        BankStatementRecord.objects.bulk_create(truly_new_records, ignore_conflicts=True)

    result_stats = {
        "total_in_file": total_in_file,
        "newly_inserted": len(truly_new_records),
        "skipped_duplicates": skipped_count,
        "credits_inserted": credits_inserted,
        "debits_inserted": debits_inserted,
    }

    if sync_job:
        sync_job.newly_inserted = len(truly_new_records)
        sync_job.skipped_duplicates = skipped_count
        sync_job.credits_inserted = credits_inserted
        sync_job.debits_inserted = debits_inserted
        sync_job.save(update_fields=["newly_inserted", "skipped_duplicates", "credits_inserted", "debits_inserted"])

    log.info(
        f"[UBL INGESTION] Total: {total_in_file} | New: {len(truly_new_records)} "
        f"(Credits: {credits_inserted}, Debits: {debits_inserted}) | Skipped duplicates: {skipped_count}"
    )
    return result_stats
