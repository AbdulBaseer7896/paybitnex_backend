"""
Invoice PDF composers — customer-facing.

Two public functions:
    compose_single_invoice(payment)   → sections for a single-transaction invoice
    compose_bulk_invoice(payments)    → sections for a multi-transaction invoice

Both return sections consumable by PDFReportBuilder.build(). These invoices
are customer-facing: they show the amount received, the fee charged, the
exchange rate applied, and the net PKR paid out — and NOTHING ELSE. No
partner splits, no profit figures, no internal annotations.
"""
from decimal import Decimal
from datetime import datetime


def _money(value, code=""):
    """Format with thousands separators and optional currency code."""
    try:
        v = Decimal(str(value or 0))
    except Exception:
        v = Decimal(0)
    prefix = f"{code} " if code else ""
    return f"{prefix}{v:,.2f}"


def _safe(obj, path, default="—"):
    """Walk a dotted path on an object; return default if anything missing."""
    try:
        for p in path.split("."):
            obj = getattr(obj, p)
            if obj is None:
                return default
        return obj if obj != "" else default
    except Exception:
        return default


def _fee_pct_str(payment):
    """Human-readable fee percentage."""
    if payment.fee_percentage is None:
        return "—"
    return f"{Decimal(payment.fee_percentage):.2f}%"


def _rate_str(payment):
    """Exchange rate written as '1 USD = 278.5000 PKR'."""
    if payment.exchange_rate is None:
        return "—"
    return f"1 {payment.currency_id} = {Decimal(payment.exchange_rate):,.4f} PKR"


# ─────────────────────────────────────────────────────────────────────
# Single-transaction invoice
# ─────────────────────────────────────────────────────────────────────
def compose_single_invoice(payment):
    """
    Sections for a single-payment invoice PDF.
    `payment` is an IncomingPayment instance.
    """
    sections = []
    cust = payment.customer

    # Top paragraph — friendly framing
    sections.append({"type": "paragraph", "text":
        "This invoice summarises the funds received, the fee charged, and "
        "the net amount disbursed in Pakistani Rupees for the transaction "
        "listed below. Please retain this document for your records."
    })

    # Transaction identity block — reference + dates
    sections.append({"type": "heading", "text": "Transaction details"})
    sections.append({"type": "table",
        "headers": ["Field", "Value"],
        "rows": [
            ["Reference",           payment.reference or "—"],
            ["Confirmation Code",   payment.external_transaction_id or "—"],
            ["Submitted on",        payment.created_at.strftime("%b %d, %Y at %H:%M")
                                    if payment.created_at else "—"],
            ["Status",              (payment.status or "—").replace("_", " ").title()],
            ["Sender",              payment.sender_name or "—"],
            ["Sender company",      payment.sender_company or "—"],
        ],
        "col_widths": [2.2, 4.6],
        "align": [None, None],
    })

    # Customer details block
    sections.append({"type": "heading", "text": "Customer"})
    sections.append({"type": "table",
        "headers": ["Field", "Value"],
        "rows": [
            ["Name",   _safe(cust, "full_name") or "—"],
            ["Email",  _safe(cust, "email") or "—"],
            ["Phone",  _safe(cust, "phone") or "—"],
        ],
        "col_widths": [2.2, 4.6],
        "align": [None, None],
    })

    # Amount breakdown — the meat of the invoice
    sections.append({"type": "heading", "text": "Amount breakdown"})

    currency_code = payment.currency_id

    sections.append({"type": "table",
        "headers": ["Item", "Amount"],
        "rows": [
            ["Amount received",
             _money(payment.amount, currency_code)],
            [f"Fee charged ({_fee_pct_str(payment)})",
             _money(payment.fee_amount_foreign, currency_code)
                 if payment.fee_amount_foreign is not None else "—"],
            ["Net amount after fee",
             _money(payment.net_amount_foreign, currency_code)
                 if payment.net_amount_foreign is not None else "—"],
            ["Exchange rate applied",
             _rate_str(payment)],
        ],
        "total_row": [
            "NET DISBURSED (PKR)",
            _money(payment.net_pkr, "PKR") if payment.net_pkr is not None else "—",
        ],
        "col_widths": [4.6, 2.2],
        "align": [None, "right"],
    })

    # Closing note
    sections.append({"type": "spacer", "height": 20})
    sections.append({"type": "paragraph", "text":
        "<i>This invoice is auto-generated from our records. For any query "
        "regarding this transaction, please refer to the reference number "
        "shown above.</i>"
    })

    return sections


# ─────────────────────────────────────────────────────────────────────
# Bulk invoice — one PDF, many transactions
# ─────────────────────────────────────────────────────────────────────
def compose_bulk_invoice(payments):
    """
    Sections for a bulk invoice covering multiple payments.
    `payments` is a list/queryset of IncomingPayment.
    """
    sections = []
    count = len(payments)

    # Detect if all payments share the same customer — if so, show their
    # name on the intro; otherwise just generalise.
    customers = {p.customer_id: p.customer for p in payments}
    single_customer = len(customers) == 1
    cust_obj = next(iter(customers.values())) if single_customer else None

    # Intro paragraph
    if single_customer and cust_obj:
        sections.append({"type": "paragraph", "text":
            f"This invoice summarises <b>{count}</b> transaction"
            f"{'s' if count != 1 else ''} for "
            f"<b>{_safe(cust_obj, 'full_name') or _safe(cust_obj, 'email')}</b>. "
            "Each row shows the amount received, the fee charged, the "
            "exchange rate applied, and the net PKR disbursed."
        })
    else:
        sections.append({"type": "paragraph", "text":
            f"This invoice summarises <b>{count}</b> transaction"
            f"{'s' if count != 1 else ''}. Each row shows the amount "
            "received, the fee charged, the exchange rate applied, and "
            "the net PKR disbursed."
        })

    # Customer block (only if single customer)
    if single_customer and cust_obj:
        sections.append({"type": "heading", "text": "Customer"})
        sections.append({"type": "table",
            "headers": ["Field", "Value"],
            "rows": [
                ["Name",   _safe(cust_obj, "full_name") or "—"],
                ["Email",  _safe(cust_obj, "email") or "—"],
                ["Phone",  _safe(cust_obj, "phone") or "—"],
            ],
            "col_widths": [2.2, 4.6],
            "align": [None, None],
        })

    # Transactions table — the main content
    sections.append({"type": "heading",
                     "text": f"Transactions ({count})"})

    table_rows = []
    total_net_pkr = Decimal(0)

    for p in payments:
        # Customer column shown only when mixed customers
        cust_cell = (
            ""  # empty when single-customer, we already showed the block above
            if single_customer
            else (_safe(p.customer, "full_name")
                  or _safe(p.customer, "email") or "—")
        )

        date_str = p.created_at.strftime("%Y-%m-%d") if p.created_at else "—"

        amount_str = f"{Decimal(p.amount or 0):,.2f} {p.currency_id}"
        fee_str = (
            f"{Decimal(p.fee_amount_foreign):,.2f} {p.currency_id}"
            if p.fee_amount_foreign is not None else "—"
        )
        rate_str = (
            f"{Decimal(p.exchange_rate):,.4f}"
            if p.exchange_rate is not None else "—"
        )
        net_pkr_str = (
            f"{Decimal(p.net_pkr):,.2f}"
            if p.net_pkr is not None else "—"
        )

        if p.net_pkr is not None:
            total_net_pkr += Decimal(p.net_pkr)

        if single_customer:
            # Columns: Ref, Date, Amount+Cur, Fee%, Fee Amount, Rate, Net PKR
            table_rows.append([
                p.reference or "—",
                date_str,
                amount_str,
                _fee_pct_str(p),
                fee_str,
                rate_str,
                net_pkr_str,
            ])
        else:
            # Include a Customer column; drop the bank/sender to keep it readable
            table_rows.append([
                p.reference or "—",
                cust_cell,
                date_str,
                amount_str,
                fee_str,
                rate_str,
                net_pkr_str,
            ])

    if single_customer:
        headers = ["Reference", "Date", "Amount received", "Fee %",
                   "Fee amount", "Rate (PKR)", "Net PKR"]
        total_row = ["TOTAL NET DISBURSED", "", "", "", "", "",
                     f"{total_net_pkr:,.2f}"]
        col_widths = [1.3, 0.9, 1.3, 0.6, 1.1, 0.9, 0.8]
        align = [None, None, "right", "right", "right", "right", "right"]
    else:
        headers = ["Reference", "Customer", "Date", "Amount received",
                   "Fee amount", "Rate", "Net PKR"]
        total_row = ["TOTAL NET DISBURSED", "", "", "", "", "",
                     f"{total_net_pkr:,.2f}"]
        col_widths = [1.2, 1.4, 0.8, 1.2, 1.0, 0.7, 0.8]
        align = [None, None, None, "right", "right", "right", "right"]

    sections.append({"type": "table",
        "headers": headers,
        "rows": table_rows,
        "total_row": total_row,
        "col_widths": col_widths,
        "align": align,
    })

    # Summary stripe — per-currency totals
    by_currency = {}
    for p in payments:
        code = p.currency_id
        bucket = by_currency.setdefault(code, {
            "count": 0,
            "amount": Decimal(0),
            "fee": Decimal(0),
            "net_pkr": Decimal(0),
        })
        bucket["count"] += 1
        bucket["amount"] += Decimal(p.amount or 0)
        if p.fee_amount_foreign is not None:
            bucket["fee"] += Decimal(p.fee_amount_foreign)
        if p.net_pkr is not None:
            bucket["net_pkr"] += Decimal(p.net_pkr)

    sections.append({"type": "heading", "text": "Summary by currency"})
    sections.append({"type": "table",
        "headers": ["Currency", "Transactions", "Total received",
                    "Total fee", "Total net (PKR)"],
        "rows": [
            [code, str(b["count"]),
             f"{b['amount']:,.2f} {code}",
             f"{b['fee']:,.2f} {code}",
             f"{b['net_pkr']:,.2f}"]
            for code, b in by_currency.items()
        ],
        "total_row": [
            "GRAND TOTAL",
            str(sum(b["count"] for b in by_currency.values())),
            "", "",
            f"{total_net_pkr:,.2f}",
        ],
        "col_widths": [1.0, 1.2, 1.8, 1.5, 1.3],
        "align": [None, "right", "right", "right", "right"],
    })

    # Closing note
    sections.append({"type": "spacer", "height": 20})
    sections.append({"type": "paragraph", "text":
        "<i>This invoice is auto-generated from our records. For any query "
        "regarding these transactions, please refer to the individual "
        "reference numbers shown above.</i>"
    })

    return sections
