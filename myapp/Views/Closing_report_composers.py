"""
PDF composers for the Closing Report page.

Each `compose_*` function takes the computed `report` dict and returns a list
of PDFReportBuilder sections. The PDF endpoint picks the right composer
based on `?type=` and hands the sections to PDFReportBuilder.build().
"""
from decimal import Decimal


def _money(value, code="PKR"):
    """Format amount with thousands separators."""
    try:
        v = Decimal(str(value or 0))
    except Exception:
        return f"{code} 0.00"
    return f"{code} {v:,.2f}"


def _short(value, code=None):
    """Compact amount — no code prefix."""
    try:
        v = Decimal(str(value or 0))
    except Exception:
        v = Decimal(0)
    return f"{v:,.2f}"


# ─────────────────────────────────────────────────────────────────────
# 1. SIMPLE GENERAL — minimal top-line summary, in/out/profit only
# ─────────────────────────────────────────────────────────────────────
def compose_simple_general(report):
    totals = report["totals"]
    sections = []

    sections.append({"type": "paragraph", "text":
        "This report shows the top-line inflow, fees collected, and "
        "calculated net profit for the selected period. For a "
        "full breakdown with customers, partners, and expenses, "
        "download the <b>Comprehensive</b> report instead."
    })

    sections.append({"type": "heading", "text": "Top-line summary"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Transactions", "value": str(totals["tx_count"]),
         "sub": "in range"},
        {"label": "Received (PKR)", "value": _short(totals["total_received_pkr"]),
         "sub": "gross inflow"},
        {"label": "Fees collected", "value": _short(totals["total_fees_pkr"]),
         "sub": "company revenue"},
        {"label": "Net profit", "value": _short(totals["net_profit_pkr"]),
         "sub": "after partners & PKR expenses"},
    ]})

    sections.append({"type": "heading", "text": "Profit calculation"})
    sections.append({"type": "paragraph", "text":
        "Net profit is derived as: "
        "<b>Fees collected − Partner payouts − PKR expenses</b>. "
        "Only <b>completed</b> transactions contribute to profit (money we've "
        "actually disbursed to customers)."
    })

    partner_total = sum(
        (Decimal(p["total_pkr"] or 0) for p in report["partner_rollup"]),
        Decimal(0),
    )
    expense_pkr = Decimal(report["expense_totals"]["total_pkr_only"] or 0)

    sections.append({"type": "table",
        "headers": ["Line item", "Amount (PKR)"],
        "rows": [
            ["Fees collected",               _short(totals["total_fees_pkr"])],
            ["Less: Partner payouts",        f"({_short(partner_total)})"],
            ["Less: PKR expenses",           f"({_short(expense_pkr)})"],
        ],
        "total_row": ["Net profit (PKR)", _short(totals["net_profit_pkr"])],
        "col_widths": [4.2, 2.6],
        "align": [None, "right"],
    })

    return sections


# ─────────────────────────────────────────────────────────────────────
# 2. COMPLETE GENERAL — period buckets + totals + short partner + expenses
# ─────────────────────────────────────────────────────────────────────
def compose_general_full(report):
    totals = report["totals"]
    filters = report["filters"]
    sections = compose_simple_general(report)[:1]  # reuse opening paragraph

    # Top-line KPIs
    sections.append({"type": "heading", "text": "Top-line summary"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Transactions", "value": str(totals["tx_count"])},
        {"label": "Received (PKR)", "value": _short(totals["total_received_pkr"])},
        {"label": "Fees collected", "value": _short(totals["total_fees_pkr"])},
        {"label": "Net profit", "value": _short(totals["net_profit_pkr"])},
    ]})

    # Period breakdown
    sections.append({"type": "heading",
                     "text": f"Period breakdown (by {filters['period']})"})
    sections.append({"type": "paragraph", "text":
        "Each row represents one "
        f"{filters['period']} within the selected date range. "
        "Amounts in PKR."
    })
    if report["buckets"]:
        sections.append({"type": "table",
            "headers": ["Period", "TX", "Received", "Fees", "Net paid out"],
            "rows": [
                [b["period_label"], str(b["tx_count"]),
                 _short(b["total_received_pkr"]),
                 _short(b["total_fees_pkr"]),
                 _short(b["total_net_pkr"])]
                for b in report["buckets"]
            ],
            "total_row": ["TOTAL", str(totals["tx_count"]),
                          _short(totals["total_received_pkr"]),
                          _short(totals["total_fees_pkr"]),
                          _short(totals["total_net_pkr"])],
            "col_widths": [2.4, 0.7, 1.4, 1.4, 1.4],
            "align": [None, "right", "right", "right", "right"],
        })
    else:
        sections.append({"type": "paragraph",
                         "text": "<i>No transactions in this period.</i>"})

    # Profit calculation with partners + expenses summary
    partner_total = sum(
        (Decimal(p["total_pkr"] or 0) for p in report["partner_rollup"]),
        Decimal(0),
    )
    expense_pkr = Decimal(report["expense_totals"]["total_pkr_only"] or 0)

    sections.append({"type": "heading", "text": "Profit calculation"})
    sections.append({"type": "table",
        "headers": ["Line item", "Amount (PKR)"],
        "rows": [
            ["Fees collected",        _short(totals["total_fees_pkr"])],
            ["Less: Partner payouts", f"({_short(partner_total)})"],
            ["Less: PKR expenses",    f"({_short(expense_pkr)})"],
        ],
        "total_row": ["Net profit (PKR)", _short(totals["net_profit_pkr"])],
        "col_widths": [4.2, 2.6],
        "align": [None, "right"],
    })

    return sections


# ─────────────────────────────────────────────────────────────────────
# 3. CUSTOMER-WISE — top-line + full customer rollup table
# ─────────────────────────────────────────────────────────────────────
def compose_customer_wise(report):
    totals = report["totals"]
    rows = report["customer_rollup"]
    sections = []

    sections.append({"type": "paragraph", "text":
        "This report breaks down the business activity by customer for the "
        "selected date range. Customers are ranked by <b>fees collected</b> "
        "(their contribution to revenue)."
    })

    sections.append({"type": "heading", "text": "Top-line"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Customers", "value": str(len(rows)),
         "sub": "with activity"},
        {"label": "Transactions", "value": str(totals["tx_count"])},
        {"label": "Fees collected", "value": _short(totals["total_fees_pkr"])},
        {"label": "Net paid out", "value": _short(totals["total_net_pkr"])},
    ]})

    # Full table
    sections.append({"type": "heading", "text": "Customer breakdown"})
    if not rows:
        sections.append({"type": "paragraph",
                         "text": "<i>No customer activity in this period.</i>"})
        return sections

    # Keep emails readable but truncated if extreme
    def email_trim(e):
        return e if not e or len(e) < 36 else e[:33] + "…"

    table_rows = [
        [
            (r["full_name"] or email_trim(r["email"]) or "—"),
            email_trim(r["email"] or "—"),
            str(r["tx_count"]),
            _short(r["total_received_pkr"]),
            _short(r["total_fees_pkr"]),
            _short(r["total_net_pkr"]),
        ]
        for r in rows
    ]
    # Footer totals
    total_received = sum(Decimal(r["total_received_pkr"] or 0) for r in rows)
    total_fees     = sum(Decimal(r["total_fees_pkr"] or 0) for r in rows)
    total_net      = sum(Decimal(r["total_net_pkr"] or 0) for r in rows)
    total_tx       = sum(int(r["tx_count"] or 0) for r in rows)

    sections.append({"type": "table",
        "headers": ["Name", "Email", "TX", "Received", "Fees", "Net paid"],
        "rows": table_rows,
        "total_row": ["TOTAL", "", str(total_tx),
                      _short(total_received),
                      _short(total_fees),
                      _short(total_net)],
        "col_widths": [1.6, 2.1, 0.5, 1.1, 1.0, 1.0],
        "align": [None, None, "right", "right", "right", "right"],
    })

    return sections


# ─────────────────────────────────────────────────────────────────────
# 4. PARTNER-WISE — top-line + partner payout breakdown
# ─────────────────────────────────────────────────────────────────────
def compose_partner_wise(report):
    rows = report["partner_rollup"]
    sections = []

    sections.append({"type": "paragraph", "text":
        "This report breaks down partner payouts for the selected date range. "
        "Each partner's share is calculated from the immutable ledger entries "
        "at the historical snapshot percentage (i.e. the split that was in "
        "effect on the day each transaction was recorded)."
    })

    total_partner_pkr = sum(Decimal(r["total_pkr"] or 0) for r in rows)
    total_tx = sum(int(r["tx_count"] or 0) for r in rows)

    sections.append({"type": "heading", "text": "Top-line"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Partners", "value": str(len(rows)),
         "sub": "with activity"},
        {"label": "Ledger entries", "value": str(total_tx)},
        {"label": "Total payouts", "value": _short(total_partner_pkr),
         "sub": "PKR"},
        {"label": "Fees this range",
         "value": _short(report["totals"]["total_fees_pkr"]),
         "sub": "total fees collected"},
    ]})

    sections.append({"type": "heading", "text": "Partner breakdown"})
    if not rows:
        sections.append({"type": "paragraph",
                         "text": "<i>No partner ledger entries in this period.</i>"})
        return sections

    table_rows = [
        [
            r["partner_name"] or "—",
            f"{Decimal(r['current_share_pct'] or 0):.2f}%",
            str(r["tx_count"]),
            _short(r["total_pkr"]),
        ]
        for r in rows
    ]

    sections.append({"type": "table",
        "headers": ["Partner", "Current share %", "Entries", "Earned (PKR)"],
        "rows": table_rows,
        "total_row": ["TOTAL", "", str(total_tx), _short(total_partner_pkr)],
        "col_widths": [2.8, 1.7, 0.9, 1.6],
        "align": [None, "right", "right", "right"],
    })

    sections.append({"type": "heading", "text": "Note on share snapshots"})
    sections.append({"type": "paragraph", "text":
        "The <b>Current share %</b> column shows each partner's configured "
        "share right now, for reference. However the earned amounts above "
        "use the <i>historical</i> share snapshot that was stored with each "
        "ledger entry — so if the split has changed during the period, the "
        "numbers still reflect the original agreement for each transaction."
    })

    return sections


# ─────────────────────────────────────────────────────────────────────
# 5. EXPENSES — expense-only breakdown
# ─────────────────────────────────────────────────────────────────────
def compose_expenses(report):
    exp = report["expense_totals"]
    sections = []

    sections.append({"type": "paragraph", "text":
        "This report summarises company expenses for the selected date range, "
        "grouped by currency. PKR expenses are subtracted from net profit; "
        "foreign-currency expenses are listed separately and <b>not</b> "
        "converted in this report."
    })

    sections.append({"type": "heading", "text": "Summary"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Total entries", "value": str(exp["count"])},
        {"label": "PKR total", "value": _short(exp["total_pkr_only"]),
         "sub": "subtracted from profit"},
    ]})

    sections.append({"type": "heading", "text": "By currency"})
    if exp["by_currency"]:
        sections.append({"type": "table",
            "headers": ["Currency", "Entries", "Total"],
            "rows": [
                [c["currency"], str(c["count"]), _short(c["total"])]
                for c in exp["by_currency"]
            ],
            "col_widths": [1.5, 1.2, 4.1],
            "align": [None, "right", "right"],
        })
    else:
        sections.append({"type": "paragraph",
                         "text": "<i>No expenses recorded in this period.</i>"})

    return sections


# ─────────────────────────────────────────────────────────────────────
# 6. COMPREHENSIVE — everything, each on its own page
# ─────────────────────────────────────────────────────────────────────
def compose_comprehensive(report):
    sections = []

    sections.append({"type": "paragraph", "text":
        "This comprehensive report includes <b>all available breakdowns</b> "
        "for the selected date range: top-line summary, period-by-period "
        "movements, per-customer rollup, per-partner payouts, and expenses. "
        "Use this when you want a single document covering the whole picture."
    })

    # Reuse the complete-general composition
    for s in compose_general_full(report)[1:]:
        sections.append(s)

    # Page break before customer rollup
    sections.append({"type": "page_break"})
    for s in compose_customer_wise(report):
        sections.append(s)

    # Page break before partner rollup
    sections.append({"type": "page_break"})
    for s in compose_partner_wise(report):
        sections.append(s)

    # Page break before expenses
    sections.append({"type": "page_break"})
    for s in compose_expenses(report):
        sections.append(s)

    return sections


# Registry: maps the ?type= param to (title, composer_fn)
REPORT_TYPES = {
    "general-simple":  ("General Summary",          compose_simple_general),
    "general-full":    ("General Closing Report",   compose_general_full),
    "customers":       ("Customer-wise Report",     compose_customer_wise),
    "partners":        ("Partner-wise Report",      compose_partner_wise),
    "expenses":        ("Expenses Report",          compose_expenses),
    "comprehensive":   ("Comprehensive Closing Report", compose_comprehensive),
}
