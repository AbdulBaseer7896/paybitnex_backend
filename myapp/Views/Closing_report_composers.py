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


def _partner_payouts_from_pool(total_fees_pkr):
    """
    Compute partner payouts from `fees × active_pool/100`.

    We deliberately don't sum the ledger rollup here because the
    rollup can be polluted by historical entries written under
    older / inconsistent distribution math. Computing from the
    formula keeps this row consistent with `net_profit_pkr` (which
    uses the same formula in `_compute_net_profit`), and means the
    profit table always reads as
        Fees − Payouts − Expenses = Net profit
    rather than a confusing inequality where the displayed payout
    doesn't match what was deducted from net profit.
    """
    from myapp.Models.Partner_models import Partner
    pool = Decimal("0")
    for p in Partner.objects.filter(is_active=True).select_related("share"):
        share = getattr(p, "share", None)
        if share and share.percentage and share.percentage > 0:
            pool += Decimal(str(share.percentage))
    if pool > Decimal("100"):
        pool = Decimal("100")
    fees = Decimal(str(total_fees_pkr or 0))
    return (fees * pool / Decimal("100")).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────────────────────────────
# Shared profit-analysis section (gross vs net side-by-side with breakdown)
# ─────────────────────────────────────────────────────────────────────
def compose_profit_analysis(report):
    """
    Dedicated gross-vs-net profit analysis section.

    Under the pool-based fee distribution model:
      - Total fees are split between the partner pool and the company.
      - If active partner shares sum to P% of the pool, partners get P% and
        the company retains (100 - P)% of the fees.
      - Net profit = company-retained fees - expenses.

    Partner payouts are NOT subtracted again from net profit — they are
    already not part of the company's retained slice.
    """
    from decimal import Decimal
    from myapp.Models.Partner_models import Partner

    totals = report["totals"]
    expense_totals = report.get("expense_totals", {})
    partner_rollup = report.get("partner_rollup", [])

    fees_pkr = Decimal(str(totals.get("total_fees_pkr") or 0))
    net_profit_pkr = Decimal(str(totals.get("net_profit_pkr") or 0))
    partner_total_pkr = sum(
        (Decimal(str(p.get("total_pkr") or 0)) for p in partner_rollup),
        Decimal(0),
    )
    expense_total_pkr = Decimal(str(expense_totals.get("total_pkr_only") or 0))

    # Resolve the active partner pool so we can show the company's retained
    # slice transparently in the derivation table.
    pool_pct = Decimal("0")
    for p in Partner.objects.filter(is_active=True).select_related("share"):
        share = getattr(p, "share", None)
        if share and share.percentage and share.percentage > 0:
            pool_pct += Decimal(str(share.percentage))
    if pool_pct > Decimal("100"):
        pool_pct = Decimal("100")
    company_pct = Decimal("100") - pool_pct
    company_retained = (fees_pkr * company_pct / Decimal("100")).quantize(Decimal("0.01"))

    sections = []
    sections.append({"type": "heading", "text": "Profit Analysis — Gross vs Net"})
    sections.append({"type": "paragraph", "text":
        f"Fee pool split: partners own <b>{pool_pct:,.2f}%</b> of each fee; "
        f"PayBitnex retains <b>{company_pct:,.2f}%</b>. "
        f"Net profit is the company-retained slice minus expenses."})

    # Side-by-side KPI grid
    sections.append({
        "type": "kpi_grid",
        "items": [
            {"label": "Gross profit (total fees)",    "value": _money(fees_pkr)},
            {"label": f"Partners ({pool_pct:,.2f}% of fee)",
                                                       "value": _money(partner_total_pkr)},
            {"label": f"Company retains ({company_pct:,.2f}%)",
                                                       "value": _money(company_retained)},
            {"label": "Net profit (after expenses)",   "value": _money(net_profit_pkr)},
        ],
    })
    sections.append({"type": "spacer", "height": 10})

    # Breakdown table — line-by-line derivation
    sections.append({"type": "heading", "text": "Derivation"})
    sections.append({
        "type": "table",
        "headers": ["Line item", "Amount (PKR)", "Running"],
        "rows": [
            ["Gross fees collected",                   _money(fees_pkr),           _money(fees_pkr)],
            [f"Less: Partner payouts ({pool_pct:,.2f}% of fees)",
                                                       "-" + _money(partner_total_pkr),
                                                         _money(company_retained)],
            [f"Company retained ({company_pct:,.2f}%)", "",                        _money(company_retained)],
            ["Less: Expenses (PKR-denominated)",       "-" + _money(expense_total_pkr),
                                                         _money(net_profit_pkr)],
        ],
        "col_widths": [3.5, 1.4, 1.4],
        "align": ["left", "right", "right"],
        "total_row": ["Net profit (bottom line)", "", _money(net_profit_pkr)],
    })

    # Expense breakdown by currency (non-PKR shown for reference; they're
    # excluded from the net calculation above since the composer uses
    # total_pkr_only by design).
    by_currency = expense_totals.get("by_currency") or []
    if by_currency:
        sections.append({"type": "spacer", "height": 10})
        sections.append({"type": "heading", "text": "Expenses by currency"})
        rows = [
            [row["currency"], str(row["count"]),
             _money(row["total"], row["currency"])]
            for row in by_currency
        ]
        sections.append({
            "type": "table",
            "headers": ["Currency", "Count", "Total"],
            "rows": rows,
            "col_widths": [1.0, 0.8, 1.8],
            "align": ["left", "right", "right"],
        })
        sections.append({"type": "paragraph", "text":
            "<i>All expenses are converted to PKR (using the current rate "
            "for non-PKR currencies) and subtracted from net profit above.</i>"})

    # Partner payout detail — who got paid, how much, and their share
    if partner_rollup:
        sections.append({"type": "spacer", "height": 10})
        sections.append({"type": "heading", "text": "Partner payouts (gross)"})
        rows = []
        for p in partner_rollup:
            rows.append([
                p.get("partner_name") or "—",
                f'{Decimal(str(p.get("current_share_pct") or 0)):,.3f}%',
                str(p.get("tx_count") or 0),
                _money(p.get("total_pkr") or 0),
            ])
        sections.append({
            "type": "table",
            "headers": ["Partner", "Share %", "Tx", "PKR paid"],
            "rows": rows,
            "col_widths": [2.5, 1.0, 0.7, 1.8],
            "align": ["left", "right", "right", "right"],
            "total_row": ["Total", "", "", _money(partner_total_pkr)],
        })

    return sections


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
         "sub": "after partners & all expenses"},
    ]})

    sections.append({"type": "heading", "text": "Profit calculation"})
    sections.append({"type": "paragraph", "text":
        "Net profit is derived as: "
        "<b>Fees collected − Partner payouts − Expenses (PKR equivalent)</b>. "
        "Foreign-currency expenses are converted to PKR at the current rate "
        "before subtraction. Only <b>completed</b> transactions contribute to "
        "profit (money we've actually disbursed to customers)."
    })

    partner_total = _partner_payouts_from_pool(totals["total_fees_pkr"])
    # Use the PKR-equivalent of ALL expenses (PKR + foreign
    # converted at current rate). The earlier `total_pkr_only`
    # silently dropped USD/EUR expenses from the displayed row,
    # so users saw "Less: PKR expenses (0.00)" while the Net
    # profit total below was actually computed including those
    # foreign expenses — making the line items not add up to
    # the bottom line. Using `total_pkr_equivalent` keeps the
    # display consistent with the math.
    expense_pkr = Decimal(report["expense_totals"].get("total_pkr_equivalent")
                          or report["expense_totals"]["total_pkr_only"] or 0)

    sections.append({"type": "table",
        "headers": ["Line item", "Amount (PKR)"],
        "rows": [
            ["Fees collected",               _short(totals["total_fees_pkr"])],
            ["Less: Partner payouts",        f"({_short(partner_total)})"],
            # Renamed from "PKR expenses" → "Expenses (PKR equiv.)"
            # so it's clear the line includes foreign-currency
            # spend converted to PKR at current rates.
            ["Less: Expenses (PKR equiv.)",  f"({_short(expense_pkr)})"],
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
    partner_total = _partner_payouts_from_pool(totals["total_fees_pkr"])
    # Show ALL expenses (PKR + foreign converted to PKR equivalent)
    # in the "Less" line so the displayed math reconciles to
    # `Net profit`. Previously this used `total_pkr_only` while
    # the Net profit line subtracted `total_pkr_equivalent`,
    # which silently hid USD/EUR expenses from the breakdown
    # even though they were factored into the bottom-line total.
    # Using `total_pkr_equivalent` here means the column adds up.
    expense_pkr = Decimal(
        report["expense_totals"].get("total_pkr_equivalent")
        or report["expense_totals"].get("total_pkr_only")
        or 0
    )
    # If there are foreign-currency expenses included in the
    # equivalent number, label the line accordingly so the reader
    # knows what's being subtracted.
    has_foreign_exp = any(
        (r.get("currency") != "PKR")
        and Decimal(r.get("total") or 0) > 0
        for r in report["expense_totals"].get("by_currency", [])
    )
    expense_label = (
        "Less: Expenses (PKR equiv.)" if has_foreign_exp
        else "Less: PKR expenses"
    )

    sections.append({"type": "heading", "text": "Profit calculation"})
    sections.append({"type": "table",
        "headers": ["Line item", "Amount (PKR)"],
        "rows": [
            ["Fees collected",        _short(totals["total_fees_pkr"])],
            ["Less: Partner payouts", f"({_short(partner_total)})"],
            [expense_label,           f"({_short(expense_pkr)})"],
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
        "This report summarises company expenses for the selected date "
        "range, grouped by currency. All expenses are converted to PKR "
        "(using the current rate for non-PKR currencies) when subtracted "
        "from net profit. The "
        "<b>By currency</b> table below shows each currency's raw total "
        "alongside the PKR equivalent we used in the profit math."
    })

    # Use the canonical "total_pkr_equivalent" for the headline
    # number — that's what's actually deducted from net profit. The
    # legacy "total_pkr_only" hid USD/EUR/etc expenses, making this
    # report read "PKR total: 0.00" even when foreign expenses were
    # present (and being deducted from profit elsewhere). Falling
    # back to PKR-only keeps older snapshots renderable.
    pkr_equiv = exp.get("total_pkr_equivalent") or exp.get("total_pkr_only") or 0
    pkr_only = exp.get("total_pkr_only") or 0

    sections.append({"type": "heading", "text": "Summary"})
    sections.append({"type": "kpi_grid", "items": [
        {"label": "Total entries", "value": str(exp["count"])},
        {"label": "All-currency total (PKR equiv.)",
         "value": _short(pkr_equiv),
         "sub": "subtracted from profit"},
        {"label": "PKR-only subtotal",
         "value": _short(pkr_only),
         "sub": "of the above"},
    ]})

    sections.append({"type": "heading", "text": "By currency"})
    if exp["by_currency"]:
        # Show both the native total AND the PKR-equivalent so the
        # reader can verify the conversion. by_currency rows now
        # carry `total_pkr_equiv` (added when the report was built);
        # fall back to '—' when missing (legacy snapshots).
        rows = []
        for c in exp["by_currency"]:
            native_total = _short(c["total"])
            equiv = c.get("total_pkr_equiv")
            equiv_str = _short(equiv) if equiv not in (None, "", "None") else "—"
            rows.append([
                c["currency"],
                str(c["count"]),
                native_total,
                equiv_str,
            ])
        sections.append({"type": "table",
            "headers": ["Currency", "Entries", "Native total", "PKR equiv."],
            "rows": rows,
            "col_widths": [1.2, 1.0, 2.3, 2.3],
            "align": [None, "right", "right", "right"],
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
        "movements, per-customer rollup, per-partner payouts, expenses, "
        "and a dedicated gross-vs-net profit analysis. "
        "Use this when you want a single document covering the whole picture."
    })

    # Reuse the complete-general composition
    for s in compose_general_full(report)[1:]:
        sections.append(s)

    # Dedicated profit analysis page (gross vs net + derivation + breakdowns)
    sections.append({"type": "page_break"})
    for s in compose_profit_analysis(report):
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
    "profit-analysis": ("Profit Analysis",          compose_profit_analysis),
    "comprehensive":   ("Comprehensive Closing Report", compose_comprehensive),
}
