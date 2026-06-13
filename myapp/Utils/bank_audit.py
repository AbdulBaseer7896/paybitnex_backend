"""
Bank-reconciliation engine.

Two responsibilities:

  1. PARSING — turn an uploaded statement (CSV or .xlsx) into a list of
     normalised rows: {external_id, amount, date, direction, raw}.
     Each bank has its own column layout, so there's one parser per
     supported bank format plus a "generic" best-effort fallback.

  2. RECONCILING — compare the parsed statement rows against our own
     records (customer `IncomingPayment` + company `InternalTransaction`)
     for the same date window, and split everything into four buckets:
        matched / amount_mismatch / only_in_statement / only_in_system.

Matching key = a normalised external transaction id. Amounts are
compared on absolute value with a small tolerance, because banks and
our own records disagree on sign conventions (a withdrawal is -700 on
the statement but stored as a positive 700 internally).

Everything here is pure-Python and side-effect free except the DB reads
in `gather_system_records`. The views layer decides whether to persist.
"""
import csv
import io
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from django.db.models import Q

from myapp.Models.Transaction_models import IncomingPayment
from myapp.Models.InternalTx_models import InternalTransaction


# Amounts within this many currency units are treated as equal — guards
# against floating rounding when a bank exports "200.00" vs "200".
AMOUNT_TOLERANCE = Decimal("0.01")


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _clean_amount(raw):
    """Parse a money string into a Decimal of its ABSOLUTE value.

    Handles: "$200.00", "($700.00)" (accounting negatives), "-500",
    "1,509.00", "  92.75 ", "" → None.
    Returns (decimal_abs, is_negative) or (None, False) when unparseable.
    """
    if raw is None:
        return None, False
    s = str(raw).strip()
    if not s:
        return None, False

    is_negative = False
    # Accounting style negatives: (700.00)
    if s.startswith("(") and s.endswith(")"):
        is_negative = True
        s = s[1:-1]
    # Strip currency symbols, spaces, thousands separators.
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        is_negative = True
        s = s[1:]
    if not s:
        return None, False
    try:
        val = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, False
    return val.copy_abs(), is_negative


def normalize_txn_id(raw):
    """Normalise an external transaction id for matching.

    - Upper-cases.
    - Strips a leading '#' and surrounding whitespace.
    - Collapses internal whitespace.
    Cash App ids look like "#D-N77EOGP56"; we store/compare "D-N77EOGP56".
    Scientific-notation junk ("9.1E+13") and blanks return "".
    """
    if raw is None:
        return ""
    s = str(raw).strip().upper()
    if not s:
        return ""
    # Drop obvious Excel scientific-notation artefacts — they're not real ids.
    if re.fullmatch(r"\d+(\.\d+)?E\+?\d+", s):
        return ""
    s = s.lstrip("#").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_date(raw):
    """Best-effort date parse. Returns a date or None."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    # Cash App: "2026-05-30 13:36:27 EDT" → take the date portion.
    s = re.sub(r"\s+[A-Z]{2,4}$", "", s)  # drop trailing TZ abbrev
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d/%m/%Y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Last resort: leading YYYY-MM-DD or M/D/YYYY token.
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------
# Reading raw rows out of the uploaded file (CSV or XLSX)
# ---------------------------------------------------------------------

def read_table(file_obj, filename=""):
    """Read an uploaded statement into a list of dict rows keyed by header.

    Supports .csv / .tsv / .xlsx / .xls. Returns (headers, rows) where
    rows is a list of {header: cell_value} dicts. Raises ValueError on
    an unreadable / empty file.
    """
    name = (filename or getattr(file_obj, "name", "") or "").lower()
    raw = file_obj.read()
    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except Exception:
            pass

    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return _read_xlsx(raw)
    # Default: treat as delimited text.
    return _read_delimited(raw)


def _read_delimited(raw_bytes):
    if isinstance(raw_bytes, bytes):
        text = raw_bytes.decode("utf-8-sig", errors="replace")
    else:
        text = raw_bytes
    # Sniff delimiter — fall back to comma. Tab-separated exports are common.
    sample = text[:4096]
    delimiter = ","
    if sample.count("\t") > sample.count(","):
        delimiter = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    all_rows = [r for r in reader]
    # Drop fully-empty leading rows.
    while all_rows and not any((c or "").strip() for c in all_rows[0]):
        all_rows.pop(0)
    if not all_rows:
        raise ValueError("The uploaded file appears to be empty.")
    headers = [(h or "").strip() for h in all_rows[0]]
    rows = []
    for r in all_rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        row = {}
        for i, h in enumerate(headers):
            row[h] = r[i] if i < len(r) else ""
        rows.append(row)
    return headers, rows


def _read_xlsx(raw_bytes):
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover
        raise ValueError(
            "Excel parsing requires the 'openpyxl' package on the server."
        ) from e
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    all_rows = [list(r) for r in rows_iter]
    while all_rows and not any(
        (str(c).strip() if c is not None else "") for c in all_rows[0]
    ):
        all_rows.pop(0)
    if not all_rows:
        raise ValueError("The uploaded spreadsheet appears to be empty.")
    headers = [(str(h).strip() if h is not None else "") for h in all_rows[0]]
    rows = []
    for r in all_rows[1:]:
        if not any((str(c).strip() if c is not None else "") for c in r):
            continue
        row = {}
        for i, h in enumerate(headers):
            row[h] = r[i] if i < len(r) else ""
        rows.append(row)
    return headers, rows


# ---------------------------------------------------------------------
# Per-bank parsers: raw dict rows → normalised statement entries
# ---------------------------------------------------------------------
#
# A normalised entry is:
#   {
#     "external_id": "<normalised id or ''>",
#     "amount": Decimal (absolute),
#     "is_negative": bool,        # money OUT on the statement
#     "date": "YYYY-MM-DD" or None,
#     "description": "...",
#     "counterparty": "...",
#     "raw": {original row},
#   }

def _find_header(headers, *candidates):
    """Case-insensitive header lookup. Returns the actual header key or None."""
    lowered = {h.lower().strip(): h for h in headers if h}
    for c in candidates:
        key = lowered.get(c.lower().strip())
        if key:
            return key
    # Partial contains match as a fallback.
    for c in candidates:
        for hl, original in lowered.items():
            if c.lower() in hl:
                return original
    return None


def parse_cashapp(headers, rows):
    h_id = _find_header(headers, "Transaction ID")
    h_amt = _find_header(headers, "Amount")
    h_net = _find_header(headers, "Net Amount")
    h_date = _find_header(headers, "Date")
    h_status = _find_header(headers, "Status")
    h_type = _find_header(headers, "Transaction Type")
    h_party = _find_header(headers, "Name of sender/receiver", "Name")
    h_notes = _find_header(headers, "Notes")

    out = []
    for r in rows:
        ext = normalize_txn_id(r.get(h_id) if h_id else "")
        amt, neg = _clean_amount(r.get(h_amt) if h_amt else None)
        if amt is None and h_net:
            amt, neg = _clean_amount(r.get(h_net))
        if amt is None:
            continue
        out.append({
            "external_id": ext,
            "amount": amt,
            "is_negative": neg,
            "date": _date_str(_parse_date(r.get(h_date)) if h_date else None),
            "description": _s(r.get(h_type) if h_type else "") or _s(r.get(h_notes) if h_notes else ""),
            "counterparty": _s(r.get(h_party) if h_party else ""),
            "status": _s(r.get(h_status) if h_status else ""),
            "raw": _stringify_row(r),
        })
    return out


def parse_amex(headers, rows):
    # American Express: Date, Description, Status, Currency, Amount,
    # Ending Balance, Reference. There's no clean per-transaction id —
    # the Reference column is scientific-notation junk — so matching
    # here leans on amount + date and the description text.
    h_amt = _find_header(headers, "Amount")
    h_date = _find_header(headers, "Date")
    h_desc = _find_header(headers, "Description")
    h_status = _find_header(headers, "Status")
    h_ref = _find_header(headers, "Reference")

    out = []
    for r in rows:
        amt, neg = _clean_amount(r.get(h_amt) if h_amt else None)
        if amt is None:
            continue
        out.append({
            "external_id": normalize_txn_id(r.get(h_ref) if h_ref else ""),
            "amount": amt,
            "is_negative": neg,
            "date": _date_str(_parse_date(r.get(h_date)) if h_date else None),
            "description": _s(r.get(h_desc) if h_desc else ""),
            "counterparty": _extract_counterparty(_s(r.get(h_desc) if h_desc else "")),
            "status": _s(r.get(h_status) if h_status else ""),
            "raw": _stringify_row(r),
        })
    return out


def parse_usbank(headers, rows):
    # US Bank: Date, Transaction (CREDIT/DEBIT), Name, Memo, Amount.
    # No transaction id at all — reconciliation is amount + date based,
    # with the Name column carrying an embedded reference token we try
    # to extract.
    h_amt = _find_header(headers, "Amount")
    h_date = _find_header(headers, "Date")
    h_name = _find_header(headers, "Name")
    h_type = _find_header(headers, "Transaction")

    out = []
    for r in rows:
        amt, neg = _clean_amount(r.get(h_amt) if h_amt else None)
        if amt is None:
            continue
        name = _s(r.get(h_name) if h_name else "")
        out.append({
            "external_id": _extract_embedded_ref(name),
            "amount": amt,
            "is_negative": neg,
            "date": _date_str(_parse_date(r.get(h_date)) if h_date else None),
            "description": _s(r.get(h_type) if h_type else "") + (f" — {name}" if name else ""),
            "counterparty": _extract_counterparty(name),
            "status": "",
            "raw": _stringify_row(r),
        })
    return out


def parse_generic(headers, rows):
    """Best-effort parser for an unknown layout.

    Looks for the most likely id / amount / date columns by header name.
    """
    h_id = _find_header(
        headers, "Transaction ID", "Reference", "Ref", "ID", "Txn ID",
    )
    h_amt = _find_header(headers, "Amount", "Net Amount", "Value", "Debit", "Credit")
    h_date = _find_header(headers, "Date", "Transaction Date", "Posted")
    h_desc = _find_header(headers, "Description", "Memo", "Notes", "Name")

    out = []
    for r in rows:
        amt, neg = _clean_amount(r.get(h_amt) if h_amt else None)
        if amt is None:
            continue
        out.append({
            "external_id": normalize_txn_id(r.get(h_id) if h_id else ""),
            "amount": amt,
            "is_negative": neg,
            "date": _date_str(_parse_date(r.get(h_date)) if h_date else None),
            "description": _s(r.get(h_desc) if h_desc else ""),
            "counterparty": "",
            "status": "",
            "raw": _stringify_row(r),
        })
    return out


PARSERS = {
    "cashapp": parse_cashapp,
    "amex": parse_amex,
    "us_bank": parse_usbank,
    "generic": parse_generic,
}


def parse_statement(bank, file_obj, filename=""):
    """Top-level: read the file and run the bank-specific parser.

    Returns the list of normalised statement entries.
    """
    headers, rows = read_table(file_obj, filename)
    parser = PARSERS.get(bank, parse_generic)
    return parser(headers, rows)


# ---------------------------------------------------------------------
# Tiny string helpers
# ---------------------------------------------------------------------

def _s(v):
    return ("" if v is None else str(v)).strip()


def _date_str(d):
    return d.isoformat() if d else None


def _stringify_row(r):
    """Coerce a raw row dict to a JSON-safe {str: str} mapping."""
    out = {}
    for k, v in r.items():
        if k is None:
            continue
        out[str(k)] = "" if v is None else str(v)
    return out


_REF_TOKEN = re.compile(r"\b([A-Z]{2,4}[0-9A-Za-z]{6,})\b")


def _extract_embedded_ref(text):
    """Pull a bank-embedded reference token out of a US Bank name line.

    e.g. "ZELLE INSTANT PMT FROM TOOLE LOGISTICS LLC  BAChpyr0uvkk"
         → "BACHPYR0UVKK"
    Returns normalised id or "".
    """
    if not text:
        return ""
    matches = _REF_TOKEN.findall(text)
    if not matches:
        return ""
    # Prefer the last token (the reference is usually trailing).
    return normalize_txn_id(matches[-1])


def _extract_counterparty(text):
    """Heuristic: strip common bank prefixes to surface the payer name."""
    if not text:
        return ""
    t = text
    for prefix in (
        "ZELLE INSTANT PMT FROM", "ZELLE PAYMENT FROM", "ZELLE PAYMENT TO",
        "ELECTRONIC DEPOSIT", "WEB AUTHORIZED PMT",
        "Online Transfer / Payment: Debit to",
    ):
        if t.upper().startswith(prefix.upper()):
            t = t[len(prefix):].strip()
            break
    # Drop a trailing reference token if present.
    t = _REF_TOKEN.sub("", t).strip()
    return t[:120]


# ---------------------------------------------------------------------
# Gathering OUR records for the same window
# ---------------------------------------------------------------------

# When a date window is supplied we DON'T clamp the system side to the
# exact same days. Two timing realities make an exact clamp produce false
# "only in statement" / "only in system" noise:
#
#   1. `IncomingPayment` only has `created_at` — the moment the customer
#      logged the payment in our system — which can lag the real bank
#      transaction date by a few days.
#   2. Bank settlement dates drift a day or two from when the money moved.
#
# So we widen the system-side query by this many days on each end of the
# requested window. The statement side is still clipped to the exact
# window in `run_audit`, so the audit is "for May" from the user's point
# of view, but a payment that landed May 31 and was entered June 2 still
# reconciles instead of showing up as a phantom discrepancy.
from datetime import timedelta

SYSTEM_WINDOW_BUFFER_DAYS = 7


def gather_system_records(start=None, end=None):
    """Collect our own transactions in the window as normalised entries.

    Pulls from both:
      - IncomingPayment.external_transaction_id (customer flow)
      - InternalTransaction.reference          (company flow)

    Returns a list of normalised entries with the same shape as the
    statement entries plus a `source` ("incoming"/"internal") and a
    `record_id` (our PK) so the UI can deep-link.

    The window is widened by SYSTEM_WINDOW_BUFFER_DAYS on each side — see
    the module-level note above for why.
    """
    out = []

    buf_start = (start - timedelta(days=SYSTEM_WINDOW_BUFFER_DAYS)) if start else None
    buf_end = (end + timedelta(days=SYSTEM_WINDOW_BUFFER_DAYS)) if end else None

    inc_qs = IncomingPayment.objects.all()
    if buf_start:
        inc_qs = inc_qs.filter(created_at__date__gte=buf_start)
    if buf_end:
        inc_qs = inc_qs.filter(created_at__date__lte=buf_end)
    for p in inc_qs.only(
        "id", "reference", "external_transaction_id", "amount",
        "created_at", "sender_name", "status",
    ):
        out.append({
            "source": "incoming",
            "record_id": str(p.id),
            "reference": p.reference,
            "external_id": normalize_txn_id(p.external_transaction_id),
            "amount": (p.amount.copy_abs() if p.amount is not None else None),
            "date": _date_str(p.created_at.date() if p.created_at else None),
            "counterparty": p.sender_name or "",
            "status": p.status,
        })

    int_qs = InternalTransaction.objects.all()
    if buf_start:
        int_qs = int_qs.filter(occurred_on__gte=buf_start)
    if buf_end:
        int_qs = int_qs.filter(occurred_on__lte=buf_end)
    for t in int_qs.only(
        "id", "reference", "amount", "occurred_on", "description",
    ):
        out.append({
            "source": "internal",
            "record_id": str(t.id),
            "reference": t.reference or "",
            "external_id": normalize_txn_id(t.reference),
            "amount": (t.amount.copy_abs() if t.amount is not None else None),
            "date": _date_str(t.occurred_on) if t.occurred_on else None,
            "counterparty": "",
            "status": "",
        })

    return out


# ---------------------------------------------------------------------
# The reconciliation itself
# ---------------------------------------------------------------------

def _amounts_equal(a, b):
    if a is None or b is None:
        return False
    return abs(Decimal(a) - Decimal(b)) <= AMOUNT_TOLERANCE


def reconcile(statement_entries, system_entries):
    """Core matching.

    Strategy:
      1. Match on normalised external id first (the strong key). When a
         statement id equals a system id:
           - amounts agree → matched
           - amounts differ → amount_mismatch
      2. For statement/system rows with NO usable id (AMEX, US Bank, or
         blank Cash App rows), fall back to matching on (amount, date).
      3. Whatever remains unmatched on each side becomes
         only_in_statement / only_in_system.

    Returns a result dict with the four buckets + a summary.
    """
    matched = []
    amount_mismatch = []

    # Index system entries by id and by (amount,date) for the fallback.
    sys_by_id = {}
    for e in system_entries:
        if e["external_id"]:
            sys_by_id.setdefault(e["external_id"], []).append(e)

    used_system = set()      # record_ids consumed by a match
    used_statement = set()   # statement indices consumed

    # ---- Pass 1: id-based matching ----
    for idx, s in enumerate(statement_entries):
        sid = s["external_id"]
        if not sid or sid not in sys_by_id:
            continue
        # take the first not-yet-used system row with this id
        candidate = None
        for cand in sys_by_id[sid]:
            if cand["record_id"] not in used_system:
                candidate = cand
                break
        if candidate is None:
            continue
        used_system.add(candidate["record_id"])
        used_statement.add(idx)
        s_amt = s["amount"]
        c_amt = candidate["amount"]
        pair = _pair(s, candidate, s_amt, c_amt)
        if _amounts_equal(s_amt, c_amt):
            matched.append(pair)
        else:
            amount_mismatch.append(pair)

    # ---- Pass 2: (amount,date) fallback for the rest ----
    # Build an index of remaining system rows by (amount, date).
    sys_remaining = [
        e for e in system_entries if e["record_id"] not in used_system
    ]
    sys_by_amt_date = {}
    for e in sys_remaining:
        key = (_amt_key(e["amount"]), e["date"])
        sys_by_amt_date.setdefault(key, []).append(e)

    for idx, s in enumerate(statement_entries):
        if idx in used_statement:
            continue
        key = (_amt_key(s["amount"]), s["date"])
        bucket = sys_by_amt_date.get(key)
        if not bucket:
            # try amount-only (date sometimes shifts a day for settlement)
            alt = [
                e for e in sys_remaining
                if e["record_id"] not in used_system
                and _amounts_equal(e["amount"], s["amount"])
            ]
            bucket = alt or None
        if not bucket:
            continue
        candidate = next(
            (c for c in bucket if c["record_id"] not in used_system), None
        )
        if candidate is None:
            continue
        used_system.add(candidate["record_id"])
        used_statement.add(idx)
        matched.append(_pair(s, candidate, s["amount"], candidate["amount"],
                             matched_on="amount_date"))

    # ---- Leftovers ----
    only_in_statement = [
        _statement_only(s) for idx, s in enumerate(statement_entries)
        if idx not in used_statement
    ]
    only_in_system = [
        _system_only(e) for e in system_entries
        if e["record_id"] not in used_system
    ]

    summary = {
        "total_statement": len(statement_entries),
        "total_system": len(system_entries),
        "matched": len(matched),
        "amount_mismatch": len(amount_mismatch),
        "only_in_statement": len(only_in_statement),
        "only_in_system": len(only_in_system),
        "statement_total_amount": _sum_amounts(statement_entries),
        "system_total_amount": _sum_amounts(system_entries),
    }

    return {
        "summary": summary,
        "matched": matched,
        "amount_mismatch": amount_mismatch,
        "only_in_statement": only_in_statement,
        "only_in_system": only_in_system,
    }


def _amt_key(amount):
    if amount is None:
        return None
    return str(Decimal(amount).quantize(Decimal("0.01")))


def _pair(s, sysrec, s_amt, c_amt, matched_on="id"):
    return {
        "external_id": s["external_id"] or sysrec["external_id"],
        "statement_amount": _money(s_amt),
        "system_amount": _money(c_amt),
        "difference": _money(
            (Decimal(s_amt) - Decimal(c_amt)) if (s_amt is not None and c_amt is not None) else None
        ),
        "statement_date": s.get("date"),
        "system_date": sysrec.get("date"),
        "counterparty": s.get("counterparty") or sysrec.get("counterparty") or "",
        "description": s.get("description", ""),
        "source": sysrec["source"],
        "record_id": sysrec["record_id"],
        "reference": sysrec.get("reference", ""),
        "matched_on": matched_on,
    }


def _statement_only(s):
    return {
        "external_id": s["external_id"],
        "amount": _money(s["amount"]),
        "is_negative": s.get("is_negative", False),
        "date": s.get("date"),
        "description": s.get("description", ""),
        "counterparty": s.get("counterparty", ""),
        "status": s.get("status", ""),
        "raw": s.get("raw", {}),
    }


def _system_only(e):
    return {
        "external_id": e["external_id"],
        "amount": _money(e["amount"]),
        "date": e.get("date"),
        "counterparty": e.get("counterparty", ""),
        "source": e["source"],
        "record_id": e["record_id"],
        "reference": e.get("reference", ""),
        "status": e.get("status", ""),
    }


def _money(d):
    if d is None:
        return None
    return str(Decimal(d).quantize(Decimal("0.01")))


def _sum_amounts(entries):
    total = Decimal("0")
    for e in entries:
        if e.get("amount") is not None:
            total += Decimal(e["amount"])
    return str(total.quantize(Decimal("0.01")))


# ---------------------------------------------------------------------
# Orchestration helper used by the view
# ---------------------------------------------------------------------

def run_audit(bank, file_obj, filename="", start=None, end=None):
    """Parse the uploaded statement, gather our records, reconcile.

    `start` / `end` are date objects (or None). Returns the result dict.
    Raises ValueError on parse problems (caught by the view → 400).
    """
    statement_entries = parse_statement(bank, file_obj, filename)
    if not statement_entries:
        raise ValueError(
            "No usable transaction rows were found in the uploaded file. "
            "Check that you selected the correct bank format."
        )

    # If a window is given, clip statement entries to it as well so both
    # sides cover the same period (entries with no parseable date are kept).
    if start or end:
        clipped = []
        for s in statement_entries:
            d = _parse_date(s["date"]) if s["date"] else None
            if d is None:
                clipped.append(s)
                continue
            if start and d < start:
                continue
            if end and d > end:
                continue
            clipped.append(s)
        statement_entries = clipped

    system_entries = gather_system_records(start, end)
    return reconcile(statement_entries, system_entries)
