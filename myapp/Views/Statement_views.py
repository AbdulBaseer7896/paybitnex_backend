"""
Bank Statement — one unified, account-centric ledger over EVERY money
movement stored in the database.

────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS
────────────────────────────────────────────────────────────────────────
The company operates 5–6 USA bank accounts (US Bank, Amex, Cash App,
Airwallex, Chase, …), several credit cards, and internal Pakistani bank
accounts. Money moves through them in four distinct flows, each stored
in a different table:

  1. Customer payments  (IncomingPayment)  — money IN via a payment
     method (Zelle / Cash App / ACH-Wire / Payoneer). In US banking a
     method always resolves to one account: Zelle is enrolled to a single
     bank account, Cash App's balance IS an account, wires target a
     specific routing/account number. We attribute each payment through
     PaymentMethod.deposit_account (set in Settings → Payment methods).

  2. Internal transactions (InternalTransaction) — company money moving
     between our own accounts, to vendors, or USA→PK. Double-entry:
     an internal transfer produces an OUT entry on the source account
     AND an IN entry on the destination account.

  3. Card spend (InternalTransaction with source_type=credit_card) —
     OUT on the card.

  4. PKR payouts (OutgoingPKRTransfer) — rupees leaving our Pakistani
     banks to customers.

This module flattens all of them into one entry shape so the frontend
can render a real bank statement per account (or across all accounts),
with direction / type / method filters and per-account totals.

────────────────────────────────────────────────────────────────────────
ENTRY SHAPE
────────────────────────────────────────────────────────────────────────
{
  id:            "pay:<uuid>" | "itx:<uuid>:out" | "itx:<uuid>:in" | "pkr:<uuid>",
  date:          "YYYY-MM-DD",         # business date
  datetime:      ISO,                  # tie-breaker for stable ordering
  direction:     "in" | "out",
  type:          "customer_payment" | "internal_transfer" | "vendor_payment"
               | "card_spend" | "usa_pk_transfer" | "pkr_payout",
  amount:        "123.45",
  currency:      "USD",
  account:       {kind: "usa"|"card"|"pk", id: uuid|null, label, bank},
                 #   kind=usa + id=null → "Unassigned" (method not mapped)
                 #   kind=pk  + id=null → PK banks as a group (payouts)
  counterparty:  "who the money came from / went to",
  counterparty_detail: extra line (company, bank name, …),
  method:        "zelle"|"cashapp"|"wire"|"ach"|"card"|"payoneer"|"other"|<code>,
  method_label:  display label,
  status:        payment status code or "" (non-payment rows),
  reference / description,
  fee:           "12.34" or None (fee attached to this movement),
  pkr_value:     rupee figure where meaningful (net_pkr / landed PKR
                 / card conversion), else None,
  peer_account:  other side of an internal transfer (label), else None,
}

Filters (query params):
  account    all | usa | card | pk | usa:<uuid> | card:<uuid> | pk:<uuid>
             | usa:unassigned
  direction  all | in | out
  type       comma list of entry types
  method     method code (matches customer-payment method OR internal
             transaction method)
  status     all | not_rejected (default) | <payment status>
  q          search reference / counterparty / description
  date_from / date_to   YYYY-MM-DD (business date)
  page / page_size
  export=csv → full filtered set as a CSV download

Response:
  { summary, accounts, results, count, page, page_size }

Design note on scale: sources are merged in Python after per-table SQL
filtering (date window, account FK, method) so the DB does the heavy
narrowing; the merge only sees the already-filtered window. At this
product's volume (thousands of rows/window) this is comfortably fast and
keeps the four schemas from being force-fitted into one SQL UNION.
"""
import csv
import io
from datetime import date as _date
from decimal import Decimal

from django.db.models import Q
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Auth_models import UserRole
from myapp.Models.Core_models import PaymentMethod
from myapp.Models.InternalTx_models import (
    CreditCard, InternalPakistaniAccount, InternalTransaction, USABankAccount,
)
from myapp.Models.Transaction_models import (
    IncomingPayment, OutgoingPKRTransfer, TransactionStatus,
)

# ── Entry types ───────────────────────────────────────────────────────
TYPE_CUSTOMER   = "customer_payment"
TYPE_TRANSFER   = "internal_transfer"
TYPE_VENDOR     = "vendor_payment"
TYPE_CARD       = "card_spend"
TYPE_USA_PK     = "usa_pk_transfer"
TYPE_PKR_PAYOUT = "pkr_payout"
ALL_TYPES = {
    TYPE_CUSTOMER, TYPE_TRANSFER, TYPE_VENDOR,
    TYPE_CARD, TYPE_USA_PK, TYPE_PKR_PAYOUT,
}

# Map free-form PaymentMethod codes onto the icon vocabulary the frontend
# knows. Admin-created methods keep their own code (frontend falls back to
# a generic icon) but the seeded ones normalise cleanly.
def _norm_method(code):
    c = (code or "").lower().replace("-", "_").replace(" ", "_")
    if "zelle" in c:
        return "zelle"
    if "cash" in c:            # cashapp / cash_app
        return "cashapp"
    if "payoneer" in c:
        return "payoneer"
    if "wire" in c and "ach" in c:
        return "wire"          # combined "ACH / Wire" method
    if "wire" in c:
        return "wire"
    if "ach" in c:
        return "ach"
    if "card" in c:
        return "card"
    return c or "other"


def _s(v):
    """Decimal/None → str for JSON, keeping '' out of numeric fields."""
    if v is None:
        return None
    return str(v)


def _acct_usa(bank_row):
    if bank_row is None:
        return {"kind": "usa", "id": None, "label": "Unassigned", "bank": "other"}
    return {
        "kind": "usa",
        "id": str(bank_row["id"]),
        "label": bank_row["label"],
        "bank": bank_row["bank"],
    }


def _norm_label(v):
    """Loose label normaliser for card ↔ bank matching."""
    return "".join(ch for ch in (v or "").lower() if ch.isalnum())


# Cards don't carry a DB link to a USA bank account, but operators name
# them after the bank they draw on ("US Bank Card" → "US Bank"). We treat a
# card as belonging to a USA bank when the bank's label is contained in the
# card's label (or vice-versa), so selecting that bank surfaces its card
# spend. Built once per request and cached on the module-level call site.
def _card_bank_map():
    """credit_card_id (str) → {"id","label","bank"} of the matched USA bank."""
    banks = list(USABankAccount.objects.filter(is_active=True))
    norm_banks = [(_norm_label(b.label), b) for b in banks]
    out = {}
    for c in CreditCard.objects.all():
        cl = _norm_label(c.label)
        if not cl:
            continue
        match = None
        # Prefer the longest bank label that is a substring of the card
        # label (or vice-versa) so "US Bank" wins over a shorter "US".
        for nb, b in sorted(norm_banks, key=lambda x: -len(x[0])):
            if not nb:
                continue
            if nb in cl or cl in nb:
                match = b
                break
        if match:
            out[str(c.id)] = {
                "id": str(match.id),
                "label": match.label,
                "bank": match.bank,
            }
    return out


# ──────────────────────────────────────────────────────────────────────
# Source builders. Each yields entry dicts already narrowed by SQL to the
# requested window / account / method, so the merge stays small.
# ──────────────────────────────────────────────────────────────────────

def _customer_entries(f, method_map):
    """IncomingPayment → IN entries attributed via method.deposit_account."""
    if TYPE_CUSTOMER not in f["types"] or f["direction"] == "out":
        return []
    # Account narrowing: customer money only ever lands in USA accounts.
    if f["acct_kind"] in ("card", "pk"):
        return []

    qs = IncomingPayment.objects.all()
    if f["status"] == "not_rejected":
        qs = qs.exclude(status=TransactionStatus.REJECTED)
    elif f["status"] != "all":
        qs = qs.filter(status=f["status"])

    if f["date_from"]:
        qs = qs.filter(
            Q(occurred_on__gte=f["date_from"])
            | Q(occurred_on__isnull=True, created_at__date__gte=f["date_from"])
        )
    if f["date_to"]:
        qs = qs.filter(
            Q(occurred_on__lte=f["date_to"])
            | Q(occurred_on__isnull=True, created_at__date__lte=f["date_to"])
        )

    if f["acct_id"] is not None or f["acct_unassigned"]:
        # Specific USA bank (or the unassigned bucket): keep only payments
        # whose method maps there.
        if f["acct_unassigned"]:
            codes = [c for c, m in method_map.items() if m["bank"] is None]
        else:
            codes = [
                c for c, m in method_map.items()
                if m["bank"] and m["bank"]["id"] == f["acct_id"]
            ]
        if not codes:
            return []
        qs = qs.filter(payment_method_id__in=codes)

    if f["method"]:
        codes = [c for c in method_map if _norm_method(c) == f["method"]]
        qs = qs.filter(payment_method_id__in=codes or [f["method"]])

    if f["q"]:
        qs = qs.filter(
            Q(reference__icontains=f["q"])
            | Q(sender_name__icontains=f["q"])
            | Q(sender_company__icontains=f["q"])
            | Q(external_transaction_id__icontains=f["q"])
        )

    rows = qs.values(
        "id", "reference", "amount", "currency_id", "status",
        "occurred_on", "created_at", "sender_name", "sender_company",
        "payment_method_id", "net_pkr", "fee_amount_foreign",
        "external_transaction_id",
    )

    out = []
    for r in rows:
        m = method_map.get(r["payment_method_id"]) or {}
        bank = m.get("bank")
        d = r["occurred_on"] or r["created_at"].date()
        out.append({
            "id": f"pay:{r['id']}",
            "date": d.isoformat(),
            "datetime": r["created_at"].isoformat(),
            "direction": "in",
            "type": TYPE_CUSTOMER,
            "amount": _s(r["amount"]),
            "currency": r["currency_id"],
            "account": _acct_usa(bank),
            "counterparty": r["sender_name"] or r["sender_company"] or "Customer payment",
            "counterparty_detail": r["sender_company"]
                                   if r["sender_name"] and r["sender_company"] else "",
            "method": _norm_method(r["payment_method_id"]),
            "method_label": m.get("label") or (r["payment_method_id"] or "—"),
            "status": r["status"],
            "reference": r["reference"],
            "description": (f"Ext ID {r['external_transaction_id']}"
                            if r["external_transaction_id"] else ""),
            "fee": _s(r["fee_amount_foreign"]),
            "pkr_value": _s(r["net_pkr"]),
            "peer_account": None,
        })
    return out


def _internal_entries(f, card_bank_map=None):
    """InternalTransaction → OUT leg on source, IN leg on destination."""
    card_bank_map = card_bank_map or {}
    want_out = f["direction"] in ("all", "out")
    want_in = f["direction"] in ("all", "in")

    # Card spends whose card is named after the selected USA bank are
    # surfaced under that bank too (see _card_bank_map).
    cards_for_selected_bank = [
        cid for cid, b in card_bank_map.items()
        if b["id"] == f["acct_id"]
    ] if f["acct_kind"] == "usa" and f["acct_id"] is not None else []

    qs = InternalTransaction.objects.select_related(
        "source_usa_bank", "source_credit_card",
        "dest_usa_bank", "dest_vendor", "dest_pk_bank",
    )
    if f["date_from"]:
        qs = qs.filter(occurred_on__gte=f["date_from"])
    if f["date_to"]:
        qs = qs.filter(occurred_on__lte=f["date_to"])
    if f["method"] and f["method"] in ("wire", "ach", "card", "other"):
        qs = qs.filter(method=f["method"])
    elif f["method"]:
        # A customer-payment-only method (zelle etc.) — internal rows can't match.
        return []
    if f["q"]:
        qs = qs.filter(
            Q(reference__icontains=f["q"])
            | Q(description__icontains=f["q"])
            | Q(dest_vendor__name__icontains=f["q"])
            | Q(source_usa_bank__label__icontains=f["q"])
            | Q(dest_usa_bank__label__icontains=f["q"])
            | Q(dest_pk_bank__label__icontains=f["q"])
            | Q(source_credit_card__label__icontains=f["q"])
        )

    # Account narrowing at SQL level: any leg touching the account.
    if f["acct_id"] is not None:
        if f["acct_kind"] == "usa":
            usa_q = (Q(source_usa_bank_id=f["acct_id"])
                     | Q(dest_usa_bank_id=f["acct_id"]))
            # …plus card spend on a card named after this bank.
            if cards_for_selected_bank:
                usa_q |= Q(source_type="credit_card",
                           source_credit_card_id__in=cards_for_selected_bank)
            qs = qs.filter(usa_q)
        elif f["acct_kind"] == "card":
            qs = qs.filter(source_credit_card_id=f["acct_id"])
        elif f["acct_kind"] == "pk":
            qs = qs.filter(dest_pk_bank_id=f["acct_id"])
    elif f["acct_kind"] == "card":
        qs = qs.filter(source_type="credit_card")
    elif f["acct_kind"] == "pk":
        qs = qs.filter(destination_type="pk_bank")
    elif f["acct_unassigned"]:
        return []  # internal rows always have a concrete account

    method_labels = {"wire": "Wire", "ach": "ACH", "card": "Card",
                     "other": "Internal"}

    def entry_type(tx):
        if tx.source_type == "credit_card":
            return TYPE_CARD
        if tx.destination_type == "pk_bank":
            return TYPE_USA_PK
        if tx.destination_type == "vendor":
            return TYPE_VENDOR
        return TYPE_TRANSFER

    out = []
    for tx in qs:
        etype = entry_type(tx)
        if etype not in f["types"]:
            continue

        base = {
            "datetime": tx.created_at.isoformat(),
            "date": tx.occurred_on.isoformat(),
            "method": tx.method,
            "method_label": method_labels.get(tx.method, tx.method),
            "status": "",
            "reference": tx.reference or "",
            "description": tx.description or "",
        }

        # Destination label for the OUT leg's counterparty.
        if tx.destination_type == "vendor":
            dest_label = tx.dest_vendor.name if tx.dest_vendor else "Vendor"
        elif tx.destination_type == "pk_bank":
            dest_label = (f"{tx.dest_pk_bank.label} ({tx.dest_pk_bank.bank_name})"
                          if tx.dest_pk_bank else "Pakistani bank")
        else:
            dest_label = tx.dest_usa_bank.label if tx.dest_usa_bank else "USA bank"

        # ── OUT leg (source side) ────────────────────────────────────
        if want_out:
            # A card spend can also "belong" to the USA bank the card is
            # named after, so selecting that bank shows its card spend.
            card_bank = None
            if tx.source_type == "credit_card":
                src_acct = {
                    "kind": "card",
                    "id": str(tx.source_credit_card_id) if tx.source_credit_card_id else None,
                    "label": tx.source_credit_card.label if tx.source_credit_card else "Card",
                    "bank": (tx.source_credit_card.brand
                             if tx.source_credit_card else "other"),
                }
                card_bank = card_bank_map.get(src_acct["id"])
            else:
                src_acct = {
                    "kind": "usa",
                    "id": str(tx.source_usa_bank_id) if tx.source_usa_bank_id else None,
                    "label": tx.source_usa_bank.label if tx.source_usa_bank else "USA bank",
                    "bank": tx.source_usa_bank.bank if tx.source_usa_bank else "other",
                }
            # When a specific account is selected, only legs ON that account
            # (or, for card spend, on the USA bank the card is named after).
            src_matches = (
                f["acct_id"] is None
                or (src_acct["kind"] == f["acct_kind"] and src_acct["id"] == f["acct_id"])
                or (card_bank is not None
                    and f["acct_kind"] == "usa"
                    and card_bank["id"] == f["acct_id"])
            )
            kind_ok = (
                f["acct_kind"] in ("all", src_acct["kind"])
                or (card_bank is not None and f["acct_kind"] == "usa"
                    and card_bank["id"] == f["acct_id"])
            )
            if src_matches and kind_ok:
                pkr_val = None
                if etype == TYPE_USA_PK:
                    pkr_val = _s(tx.pk_amount_pkr)
                elif etype == TYPE_CARD:
                    pkr_val = _s(tx.card_profit_pkr)  # PKR received (legacy name)
                out.append({
                    **base,
                    "id": f"itx:{tx.id}:out",
                    "direction": "out",
                    "type": etype,
                    "amount": _s(tx.amount),
                    "currency": tx.currency_id,
                    "account": src_acct,
                    "counterparty": dest_label,
                    "counterparty_detail": "",
                    "fee": _s(tx.fee_amount) if tx.fee_amount else None,
                    "pkr_value": pkr_val,
                    "peer_account": dest_label if etype == TYPE_TRANSFER else None,
                })

        # ── IN leg (destination side, company accounts only) ────────
        if want_in:
            in_entry = None
            if tx.destination_type == "usa_bank" and tx.dest_usa_bank_id:
                dest_acct = {
                    "kind": "usa",
                    "id": str(tx.dest_usa_bank_id),
                    "label": tx.dest_usa_bank.label,
                    "bank": tx.dest_usa_bank.bank,
                }
                src_label = (tx.source_credit_card.label
                             if tx.source_type == "credit_card" and tx.source_credit_card
                             else (tx.source_usa_bank.label
                                   if tx.source_usa_bank else "Company account"))
                in_entry = {
                    **base,
                    "id": f"itx:{tx.id}:in",
                    "direction": "in",
                    "type": TYPE_TRANSFER,
                    "amount": _s(tx.amount),
                    "currency": tx.currency_id,
                    "account": dest_acct,
                    "counterparty": src_label,
                    "counterparty_detail": "",
                    "fee": None,
                    "pkr_value": None,
                    "peer_account": src_label,
                }
            elif tx.destination_type == "pk_bank" and tx.dest_pk_bank_id:
                landed = tx.pk_amount_pkr
                if landed:
                    dest_acct = {
                        "kind": "pk",
                        "id": str(tx.dest_pk_bank_id),
                        "label": tx.dest_pk_bank.label,
                        "bank": "pk",
                    }
                    src_label = (tx.source_usa_bank.label
                                 if tx.source_usa_bank else "USA bank")
                    in_entry = {
                        **base,
                        "id": f"itx:{tx.id}:in",
                        "direction": "in",
                        "type": TYPE_USA_PK,
                        "amount": _s(landed),
                        "currency": "PKR",
                        "account": dest_acct,
                        "counterparty": src_label,
                        "counterparty_detail":
                            f"{tx.amount} {tx.currency_id} converted"
                            + (f" @ {tx.pk_conversion_rate}" if tx.pk_conversion_rate else ""),
                        "fee": _s(tx.pk_fee_amount) if tx.pk_fee_amount else None,
                        "pkr_value": None,
                        "peer_account": src_label,
                    }
            if in_entry and in_entry["type"] in f["types"]:
                dest_matches = (
                    f["acct_id"] is None
                    or (in_entry["account"]["kind"] == f["acct_kind"]
                        and in_entry["account"]["id"] == f["acct_id"])
                )
                if dest_matches and (f["acct_kind"] in ("all", in_entry["account"]["kind"])):
                    out.append(in_entry)
    return out


def _pkr_payout_entries(f):
    """OutgoingPKRTransfer → OUT entries from the PK-banks group.

    The transfer model records the CUSTOMER's receiving account, not which
    of OUR PK accounts it left from, so these attribute to the PK group as
    a whole (account.id = None). They appear under "All accounts" and the
    "Pakistani banks" group filter, but not under a single PK account.
    """
    if TYPE_PKR_PAYOUT not in f["types"] or f["direction"] == "in":
        return []
    if f["acct_kind"] not in ("all", "pk") or f["acct_id"] is not None \
            or f["acct_unassigned"]:
        return []
    if f["method"] and f["method"] not in ("other", "wire"):
        return []

    qs = OutgoingPKRTransfer.objects.select_related(
        "customer_bank_account", "customer_bank_account__bank",
        "customer_bank_account__customer",
    )
    if f["date_from"]:
        qs = qs.filter(sent_at__date__gte=f["date_from"])
    if f["date_to"]:
        qs = qs.filter(sent_at__date__lte=f["date_to"])
    if f["q"]:
        qs = qs.filter(
            Q(reference__icontains=f["q"])
            | Q(bank_transaction_id__icontains=f["q"])
            | Q(customer_bank_account__holder_name__icontains=f["q"])
        )

    out = []
    for t in qs:
        cba = t.customer_bank_account
        holder = getattr(cba, "holder_name", "") or "Customer"
        bank_name = ""
        try:
            bank_name = cba.bank.name if cba and cba.bank_id else ""
        except Exception:
            bank_name = ""
        out.append({
            "id": f"pkr:{t.id}",
            "date": t.sent_at.date().isoformat(),
            "datetime": t.sent_at.isoformat(),
            "direction": "out",
            "type": TYPE_PKR_PAYOUT,
            "amount": _s(t.amount_pkr),
            "currency": "PKR",
            "account": {"kind": "pk", "id": None,
                        "label": "Pakistani banks", "bank": "pk"},
            "counterparty": holder,
            "counterparty_detail": bank_name,
            "method": "wire",
            "method_label": "Bank transfer",
            "status": "",
            "reference": t.reference,
            "description": (f"Bank ref {t.bank_transaction_id}"
                            if t.bank_transaction_id else ""),
            "fee": None,
            "pkr_value": None,
            "peer_account": None,
        })
    return out


# ──────────────────────────────────────────────────────────────────────

def _parse_filters(request):
    p = request.query_params

    acct = (p.get("account") or "all").strip()
    acct_kind, acct_id, unassigned = "all", None, False
    if acct in ("usa", "card", "pk"):
        acct_kind = acct
    elif acct == "usa:unassigned":
        acct_kind, unassigned = "usa", True
    elif ":" in acct:
        kind, _, aid = acct.partition(":")
        if kind in ("usa", "card", "pk") and aid:
            acct_kind, acct_id = kind, aid

    types = p.get("type") or p.get("types") or ""
    types = {t.strip() for t in types.split(",") if t.strip()} & ALL_TYPES
    if not types:
        types = set(ALL_TYPES)

    direction = p.get("direction") or "all"
    if direction not in ("in", "out"):
        direction = "all"

    status_f = (p.get("status") or "not_rejected").strip()
    valid_statuses = {c for c, _ in TransactionStatus.choices}
    if status_f not in valid_statuses and status_f not in ("all", "not_rejected"):
        status_f = "not_rejected"
    # A specific payment status only makes sense for customer payments —
    # an internal transfer has no "rejected" state — so narrow the result
    # to customer rows when one is chosen.
    if status_f not in ("all", "not_rejected"):
        types = types & {TYPE_CUSTOMER}

    def _d(key):
        v = (p.get(key) or "").strip()
        if not v:
            return None
        try:
            return _date.fromisoformat(v)
        except ValueError:
            return None

    return {
        "acct_kind": acct_kind,
        "acct_id": acct_id,
        "acct_unassigned": unassigned,
        "types": types,
        "direction": direction,
        "method": (p.get("method") or "").strip().lower(),
        "status": status_f,
        "q": (p.get("q") or p.get("search") or "").strip(),
        "date_from": _d("date_from"),
        "date_to": _d("date_to"),
    }


def _method_map():
    """PaymentMethod code → {label, bank:{id,label,bank}|None}."""
    out = {}
    for m in PaymentMethod.objects.select_related("deposit_account"):
        bank = None
        if m.deposit_account_id and m.deposit_account:
            bank = {
                "id": str(m.deposit_account_id),
                "label": m.deposit_account.label,
                "bank": m.deposit_account.bank,
            }
        out[m.code] = {"label": m.label, "bank": bank}
    return out


def _summarise(entries):
    """Totals over the full filtered set (pre-pagination)."""
    inflow, outflow = {}, {}
    method_in = {}
    for e in entries:
        cur = e["currency"]
        amt = Decimal(e["amount"] or "0")
        if e["direction"] == "in":
            inflow[cur] = inflow.get(cur, Decimal("0")) + amt
            key = e["method"]
            method_in.setdefault(key, {"label": e["method_label"],
                                       "count": 0, "by_currency": {}})
            method_in[key]["count"] += 1
            bc = method_in[key]["by_currency"]
            bc[cur] = bc.get(cur, Decimal("0")) + amt
        else:
            outflow[cur] = outflow.get(cur, Decimal("0")) + amt

    def _fmt(d):
        return {k: str(v) for k, v in sorted(d.items())}

    net = {}
    for cur in set(inflow) | set(outflow):
        net[cur] = (inflow.get(cur, Decimal("0"))
                    - outflow.get(cur, Decimal("0")))

    for m in method_in.values():
        m["by_currency"] = _fmt(m["by_currency"])

    return {
        "in": _fmt(inflow),
        "out": _fmt(outflow),
        "net": _fmt(net),
        "count": len(entries),
        "in_count": sum(1 for e in entries if e["direction"] == "in"),
        "out_count": sum(1 for e in entries if e["direction"] == "out"),
        "by_method_in": method_in,
    }


def _account_rail(f, method_map, card_bank_map=None):
    """Every selectable account + its in/out totals for the current window.

    Rail totals deliberately ignore the account/direction/type filters
    (only the date window + search-free base applies) so the chips stay
    stable while the user clicks around — a chip's number shouldn't
    change because you selected a different chip.
    """
    card_bank_map = card_bank_map or {}
    rail_f = {**f, "acct_kind": "all", "acct_id": None,
              "acct_unassigned": False, "direction": "all",
              "types": set(ALL_TYPES), "method": "", "q": ""}
    entries = (
        _customer_entries(rail_f, method_map)
        + _internal_entries(rail_f, card_bank_map)
        + _pkr_payout_entries(rail_f)
    )

    # Map a card id → its matched USA bank id so the bank chip totals
    # include card spend on a card named after it.
    card_to_bank_id = {cid: b["id"] for cid, b in card_bank_map.items()}

    per = {}
    for e in entries:
        a = e["account"]
        key = f"{a['kind']}:{a['id'] or ('unassigned' if a['kind'] == 'usa' else 'group')}"
        slot = per.setdefault(key, {"in": {}, "out": {}, "count": 0})
        slot["count"] += 1
        bucket = slot["in"] if e["direction"] == "in" else slot["out"]
        cur = e["currency"]
        bucket[cur] = bucket.get(cur, Decimal("0")) + Decimal(e["amount"] or "0")

        # Mirror card spend onto its matched USA bank chip's totals.
        if (e["type"] == TYPE_CARD and a["kind"] == "card"
                and a["id"] in card_to_bank_id):
            bkey = f"usa:{card_to_bank_id[a['id']]}"
            bslot = per.setdefault(bkey, {"in": {}, "out": {}, "count": 0})
            bslot["count"] += 1
            bbucket = bslot["out"]  # card spend is always OUT
            bbucket[cur] = bbucket.get(cur, Decimal("0")) + Decimal(e["amount"] or "0")

    def totals_for(key):
        slot = per.get(key, {"in": {}, "out": {}, "count": 0})
        return {
            "in": {k: str(v) for k, v in sorted(slot["in"].items())},
            "out": {k: str(v) for k, v in sorted(slot["out"].items())},
            "count": slot["count"],
        }

    usa = [{
        "kind": "usa", "id": str(b.id), "label": b.label, "bank": b.bank,
        "holder": b.holder_name, "last4": (b.account_number_last4 or "")[-4:],
        "is_active": b.is_active,
        "totals": totals_for(f"usa:{b.id}"),
    } for b in USABankAccount.objects.filter(is_active=True).order_by("bank", "label")]

    cards = [{
        "kind": "card", "id": str(c.id), "label": c.label, "bank": c.brand,
        "holder": c.holder_name,
        "last4": (c.last4 or "").replace(" ", "")[-4:],
        "is_active": c.is_active,
        "totals": totals_for(f"card:{c.id}"),
    } for c in CreditCard.objects.filter(is_active=True).order_by("label")]

    pk = [{
        "kind": "pk", "id": str(a.id), "label": a.label, "bank": "pk",
        "holder": a.account_title, "bank_name": a.bank_name,
        "last4": (a.account_number_last4 or "")[-4:],
        "is_active": a.is_active,
        "totals": totals_for(f"pk:{a.id}"),
    } for a in InternalPakistaniAccount.objects.filter(is_active=True).order_by("label")]

    unassigned = totals_for("usa:unassigned")
    pk_group_extra = totals_for("pk:group")   # PKR payouts (unattributed)

    return {
        "usa": usa,
        "cards": cards,
        "pk": pk,
        "usa_unassigned": unassigned,
        "pk_group": pk_group_extra,
        # Methods with no bank mapping — lets the UI nudge the admin.
        "unmapped_methods": [
            {"code": c, "label": m["label"]}
            for c, m in method_map.items() if m["bank"] is None
        ],
    }


def _entries_to_csv(entries):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Date", "Direction", "Type", "Account", "Counterparty",
        "Method", "Amount", "Currency", "Fee", "PKR value",
        "Status", "Reference", "Description",
    ])
    for e in entries:
        w.writerow([
            e["date"], e["direction"], e["type"],
            e["account"]["label"], e["counterparty"],
            e["method_label"], e["amount"], e["currency"],
            e["fee"] or "", e["pkr_value"] or "",
            e["status"], e["reference"], e["description"],
        ])
    return buf.getvalue()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bank_statement(request):
    if request.user.role not in (UserRole.ADMIN, UserRole.ACCOUNTANT):
        return Response({"detail": "Forbidden."},
                        status=status.HTTP_403_FORBIDDEN)

    f = _parse_filters(request)
    method_map = _method_map()
    card_bank_map = _card_bank_map()

    entries = (
        _customer_entries(f, method_map)
        + _internal_entries(f, card_bank_map)
        + _pkr_payout_entries(f)
    )
    # Newest first; datetime breaks same-day ties so order is stable.
    entries.sort(key=lambda e: (e["date"], e["datetime"]), reverse=True)

    if request.query_params.get("export") == "csv":
        csv_body = _entries_to_csv(entries)
        resp = HttpResponse(csv_body, content_type="text/csv")
        resp["Content-Disposition"] = (
            'attachment; filename="bank-statement.csv"'
        )
        return resp

    summary = _summarise(entries)
    accounts = _account_rail(f, method_map, card_bank_map)

    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(request.query_params.get("page_size", 25)), 1), 200)
    except ValueError:
        page_size = 25
    start = (page - 1) * page_size

    return Response({
        "summary": summary,
        "accounts": accounts,
        "count": len(entries),
        "page": page,
        "page_size": page_size,
        "results": entries[start:start + page_size],
    })
