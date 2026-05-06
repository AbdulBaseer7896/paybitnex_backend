"""
Partner views — admin-only for mutations.
Partners themselves don't log in; they're just profit-share records.

Endpoints:
    GET  /partners/                   → list partners
    POST /partners/                   → create partner (+ optional initial share)
    GET  /partners/{id}/              → detail
    PATCH/partners/{id}/              → update
    DELETE /partners/{id}/            → deactivate (soft)

    POST /partners/shares/bulk/       → re-balance all shares atomically
    GET  /partners/{id}/ledger/       → ledger entries for a partner
    GET  /partners/{id}/balance/      → running balance (PKR + per currency)
"""
from decimal import Decimal

from django.db import transaction as dbtx
from django.db.models import Sum
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Partner_models import Partner, PartnerShare, PartnerLedgerEntry
from myapp.serializers.Partner_serializers import (
    PartnerSerializer, PartnerCreateSerializer, PartnerShareSerializer,
    PartnerLedgerEntrySerializer, SharesBulkUpdateSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.partner_ledger import partner_balance


class PartnerViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = Partner.objects.all().select_related("share").order_by("name")
    serializer_class = PartnerSerializer
    search_fields = ["name", "email"]
    filterset_fields = ["is_active"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Default to active partners only on the list action. Admin can
        # pass ?is_active=false to see deactivated ones.
        if self.action == "list":
            raw = self.request.query_params.get("is_active")
            if raw is None:
                qs = qs.filter(is_active=True)
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return PartnerCreateSerializer
        return PartnerSerializer

    def create(self, request, *args, **kwargs):
        s = PartnerCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        share_pct = s.validated_data.pop("share_percentage", Decimal("0"))
        with dbtx.atomic():
            partner = Partner.objects.create(
                created_by=request.user, **s.validated_data,
            )
            PartnerShare.objects.create(
                partner=partner, percentage=share_pct, updated_by=request.user,
            )
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_CREATE, target=partner,
                description=f"Created partner {partner.name} @ {share_pct}%",
            )
        return Response(
            PartnerSerializer(partner).data, status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        partner = self.get_object()
        partner.is_active = False
        partner.save(update_fields=["is_active", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_DELETE, target=partner,
            description=f"Deactivated partner {partner.name}",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        partner = self.get_object()
        qs = (PartnerLedgerEntry.objects
              .filter(partner=partner)
              .select_related("payment", "payment__customer"))
        page = self.paginate_queryset(qs)
        ser = PartnerLedgerEntrySerializer(page or qs, many=True)
        if page is not None:
            return self.get_paginated_response(ser.data)
        return Response(ser.data)

    @action(detail=True, methods=["get"])
    def balance(self, request, pk=None):
        partner = self.get_object()
        per_currency = (
            PartnerLedgerEntry.objects
            .filter(partner=partner)
            .values("currency_code")
            .annotate(total=Sum("amount_foreign"))
        )
        return Response({
            "partner": str(partner.id),
            "total_pkr": str(partner_balance(partner, "PKR")),
            "by_currency": {
                r["currency_code"]: str(r["total"]) for r in per_currency
            },
        })

    @action(
        detail=False, methods=["post"], url_path="recompute-ledger",
        permission_classes=[IsAuthenticated, IsAdmin],
    )
    def recompute_ledger(self, request):
        """
        Re-run pro-rata distribution on every historical completed payment.

        Uses CURRENT partner shares (not historical snapshots) because
        historical snapshots are polluted from earlier bugs where all partners
        got the same wrong percentage. The pro-rata formula gives each partner
        (their_share ÷ pool_total) × fee — 100% of every fee distributed.

        Example: Huzair=3%, jhole=4%, Nagris=7% → pool=14.
          Huzair: 3/14 × fee = 21.43% of fee
          jhole:  4/14 × fee = 28.57% of fee
          Nagris: 7/14 × fee = 50.00% of fee

        Returns {updated, scanned, affected_payments}.
        """
        from decimal import Decimal, ROUND_HALF_UP
        from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus

        QUANT = Decimal("0.01")
        def _q(x):
            return Decimal(str(x)).quantize(QUANT, rounding=ROUND_HALF_UP)

        # Always use CURRENT shares — historical snapshots may be corrupted
        # from earlier incorrect distributions where every partner got 4%.
        # The snapshot stored per ledger entry is updated below to match.
        current_partners = [
            (p.id, Decimal(str(p.share.percentage)))
            for p in Partner.objects.filter(is_active=True).select_related("share")
            if getattr(p, "share", None) and p.share.percentage
            and Decimal(str(p.share.percentage)) > 0
        ]

        if not current_partners:
            return Response({"updated": 0, "scanned": 0, "affected_payments": [],
                             "detail": "No active partners with shares configured."})

        pool_total = sum(pct for _, pct in current_partners)

        qs = IncomingPayment.objects.filter(
            status__in=[TransactionStatus.COMPLETED, TransactionStatus.PKR_SENT],
        ).exclude(fee_amount_foreign__isnull=True).exclude(exchange_rate__isnull=True)

        scanned = qs.count()
        updated = 0
        affected = []

        for payment in qs:
            fee_f = _q(payment.fee_amount_foreign)
            fee_p = _q(fee_f * payment.exchange_rate)

            # Compute what the entries SHOULD be (pro-rata of current shares)
            alloc_f = Decimal("0")
            alloc_p = Decimal("0")
            target = []
            for idx, (pid, pct) in enumerate(current_partners):
                is_last = (idx == len(current_partners) - 1)
                if is_last:
                    amt_f = _q(fee_f - alloc_f)
                    amt_p = _q(fee_p - alloc_p)
                else:
                    frac = pct / pool_total
                    amt_f = _q(fee_f * frac)
                    amt_p = _q(fee_p * frac)
                    alloc_f += amt_f
                    alloc_p += amt_p
                target.append((pid, pct, amt_f, amt_p))

            # Compare with existing — rebuild only if something differs
            existing = {e.partner_id: e
                        for e in PartnerLedgerEntry.objects.filter(payment=payment)}
            needs_update = (
                len(existing) != len(target)
                or any(
                    pid not in existing
                    or _q(existing[pid].amount_foreign) != amt_f
                    or _q(existing[pid].amount_pkr) != amt_p
                    for pid, _, amt_f, amt_p in target
                )
            )

            if not needs_update:
                continue

            with dbtx.atomic():
                PartnerLedgerEntry.objects.filter(payment=payment).delete()
                for pid, pct, amt_f, amt_p in target:
                    PartnerLedgerEntry.objects.create(
                        partner_id=pid,
                        payment=payment,
                        share_snapshot=pct,   # store current share as snapshot
                        fee_total_foreign=fee_f,
                        fee_total_pkr=fee_p,
                        amount_foreign=amt_f,
                        amount_pkr=amt_p,
                        currency_code=payment.currency_id,
                    )
            updated += 1
            affected.append(payment.reference)

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE,
            description=(
                f"Recomputed partner ledger (pro-rata) for {updated} payments "
                f"(pool={pool_total}%, {len(current_partners)} partners)"
            ),
            after={"affected_references": affected[:50]},
        )
        return Response({
            "updated": updated,
            "scanned": scanned,
            "affected_payments": affected,
        })

    @action(
        detail=True, methods=["get"], url_path="report.pdf",
        permission_classes=[IsAuthenticated, IsAdmin],
    )
    def report_pdf(self, request, pk=None):
        """
        Downloadable branded PDF report for a single partner.

        Query params (all optional; all match the frontend filter bar):
            date_from, date_to      — ISO YYYY-MM-DD bounds on ledger created_at
            currency                — USD/EUR/GBP/… limit ledger to one currency
            profit_type             — gross (default) | net
            expense_split           — equal | by-share (only used in net mode)
            range_label             — human-readable label for report subtitle

        The response is a PDF attachment. On success the Content-Disposition
        header ensures the browser triggers a download (matching the Closing
        Reports pattern — no new-tab, no manual Ctrl+P).
        """
        from datetime import datetime, date
        from decimal import Decimal, ROUND_HALF_UP
        from django.http import HttpResponse
        from myapp.Utils.pdf_report import PDFReportBuilder

        partner = self.get_object()

        # ── Parse filters from query string ───────────────────────────
        def _parse_date(s):
            if not s:
                return None
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except ValueError:
                return None
        date_from = _parse_date(request.query_params.get("date_from"))
        date_to = _parse_date(request.query_params.get("date_to"))
        currency = (request.query_params.get("currency") or "").strip() or None
        profit_type = (request.query_params.get("profit_type") or "gross").lower()
        expense_split = (request.query_params.get("expense_split") or "by-share").lower()
        range_label = (request.query_params.get("range_label") or "").strip()
        if profit_type not in ("gross", "net"):
            profit_type = "gross"
        if expense_split not in ("equal", "by-share"):
            expense_split = "by-share"

        # ── Ledger for this partner, within date range ────────────────
        ledger_qs = (
            PartnerLedgerEntry.objects
            .filter(partner=partner)
            .select_related("payment", "payment__customer")
        )
        if date_from:
            ledger_qs = ledger_qs.filter(created_at__date__gte=date_from)
        if date_to:
            ledger_qs = ledger_qs.filter(created_at__date__lte=date_to)
        if currency:
            ledger_qs = ledger_qs.filter(currency_code=currency)
        ledger = list(ledger_qs.order_by("-created_at"))

        # ── Aggregate by customer for the per-customer breakdown ──────
        from collections import defaultdict
        per_customer = defaultdict(
            lambda: {"name": "", "email": "", "count": 0, "pkr": Decimal("0")},
        )
        total_gross_pkr = Decimal("0")
        for e in ledger:
            cust = getattr(e.payment, "customer", None)
            cid = str(cust.id) if cust else "unknown"
            bucket = per_customer[cid]
            bucket["name"] = cust.full_name or (cust.email if cust else "Unknown")
            bucket["email"] = cust.email if cust else ""
            bucket["count"] += 1
            amt = Decimal(e.amount_pkr or 0)
            bucket["pkr"] += amt
            total_gross_pkr += amt

        # ── Expense deduction (net mode only) ─────────────────────────
        expense_deduction = Decimal("0")
        if profit_type == "net":
            # Fetch total expenses in PKR for this date range — reuse the
            # live-rate conversion from the Expense view's logic.
            from myapp.Models.Expense_models import Expense
            from myapp.Models.Rate_models import ExchangeRate
            from django.db.models import Sum
            exp_qs = Expense.objects.all()
            if date_from:
                exp_qs = exp_qs.filter(spent_on__gte=date_from)
            if date_to:
                exp_qs = exp_qs.filter(spent_on__lte=date_to)
            rates = {r.currency_id: Decimal(str(r.rate_to_pkr or 0))
                     for r in ExchangeRate.objects.all()}
            rates["PKR"] = Decimal("1")
            total_expenses_pkr = Decimal("0")
            for row in exp_qs.values("currency_id").annotate(t=Sum("amount")):
                amt = Decimal(str(row["t"] or 0))
                total_expenses_pkr += (amt * rates.get(row["currency_id"], Decimal("0")))

            # This partner's slice of those expenses. Matches the frontend
            # logic exactly — sum across all active partners equals the
            # total expenses, with this partner taking their share.
            active_partners = list(
                Partner.objects.filter(is_active=True).select_related("share"),
            )
            n = len(active_partners)
            if n > 0 and total_expenses_pkr > 0:
                if expense_split == "equal":
                    expense_deduction = (
                        total_expenses_pkr / n
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    # by-share
                    pool = sum(
                        (Decimal(str(p.share.percentage))
                         for p in active_partners
                         if getattr(p, "share", None) and p.share.percentage),
                        Decimal("0"),
                    )
                    my_share = Decimal(
                        str(partner.share.percentage)
                        if getattr(partner, "share", None) else 0,
                    )
                    if pool > 0:
                        expense_deduction = (
                            total_expenses_pkr * (my_share / pool)
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    else:
                        # Fall back to equal split
                        expense_deduction = (
                            total_expenses_pkr / n
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        net_pkr = total_gross_pkr - expense_deduction

        # ── Build the PDF ─────────────────────────────────────────────
        def money_pkr(v):
            try:
                return f"Rs {Decimal(str(v)):,.2f}"
            except Exception:
                return str(v)

        def money(v, code):
            return f"{code} {Decimal(str(v)):,.2f}"

        subtitle_parts = []
        if range_label:
            subtitle_parts.append(range_label)
        elif date_from or date_to:
            subtitle_parts.append(
                f"{date_from or 'all time'} — {date_to or 'today'}"
            )
        else:
            subtitle_parts.append("All time")
        subtitle_parts.append(
            "Net profit" if profit_type == "net" else "Gross profit"
        )
        if profit_type == "net":
            subtitle_parts.append(
                f"expenses split: {'equal' if expense_split == 'equal' else 'by share'}"
            )

        share_pct_display = (
            getattr(getattr(partner, "share", None), "percentage", 0) or 0
        )
        metadata = {
            "Partner":     partner.name,
            "Email":       partner.email or "—",
            "Share %":     f"{share_pct_display}%",
            "Distributions": str(len(ledger)),
            "Generated By":  request.user.full_name or request.user.email,
            "Generated On":  datetime.now().strftime("%b %d, %Y at %H:%M"),
        }

        sections = []

        # KPI grid
        kpi_items = [
            {"label": "Gross PKR earned", "value": money_pkr(total_gross_pkr)},
            {"label": "Distributions",    "value": str(len(ledger))},
        ]
        if profit_type == "net":
            kpi_items.append({
                "label": f"Expense deduction ({expense_split})",
                "value": "-" + money_pkr(expense_deduction),
            })
            kpi_items.append({
                "label": "Net PKR earned",
                "value": money_pkr(net_pkr),
            })
        sections.append({"type": "kpi_grid", "items": kpi_items})
        sections.append({"type": "spacer", "height": 12})

        # Per-customer breakdown
        if per_customer:
            sections.append({"type": "heading", "text": "Per-customer breakdown"})
            rows = []
            sorted_customers = sorted(
                per_customer.values(), key=lambda c: c["pkr"], reverse=True,
            )
            for c in sorted_customers:
                share_of_total = (
                    (c["pkr"] / total_gross_pkr * 100)
                    if total_gross_pkr > 0 else Decimal("0")
                )
                rows.append([
                    c["name"],
                    c["email"] or "—",
                    str(c["count"]),
                    money_pkr(c["pkr"]),
                    f"{share_of_total:.2f}%",
                ])
            sections.append({
                "type": "table",
                "headers": ["Customer", "Email", "Tx", "PKR earned", "Share of total"],
                "rows": rows,
                "col_widths": [1.8, 2.0, 0.6, 1.4, 1.1],
                "align": ["left", "left", "right", "right", "right"],
                "total_row": [
                    "Total", "", str(sum(c["count"] for c in sorted_customers)),
                    money_pkr(total_gross_pkr), "100.00%",
                ],
            })
            sections.append({"type": "spacer", "height": 12})

        # Per-transaction ledger
        if ledger:
            sections.append({"type": "heading", "text": "Per-transaction ledger"})
            rows = []
            for e in ledger[:500]:   # cap printed rows to keep PDF manageable
                cust = getattr(e.payment, "customer", None)
                rows.append([
                    e.created_at.strftime("%Y-%m-%d") if e.created_at else "—",
                    e.payment.reference or "—",
                    cust.full_name if cust else "—",
                    e.currency_code or "—",
                    money(e.amount_foreign or 0, e.currency_code or ""),
                    f"{e.share_snapshot}%",
                    money_pkr(e.amount_pkr or 0),
                ])
            sections.append({
                "type": "table",
                "headers": ["Date", "Ref", "Customer", "Cur", "Your cut", "Share", "PKR"],
                "rows": rows,
                "col_widths": [0.9, 0.9, 1.7, 0.5, 1.1, 0.7, 1.1],
                "align": ["left", "left", "left", "left", "right", "right", "right"],
            })
            if len(ledger) > 500:
                sections.append({
                    "type": "paragraph",
                    "text": f"(showing first 500 of {len(ledger)} entries — export CSV for full list)",
                })

        if profit_type == "net":
            sections.append({"type": "spacer", "height": 10})
            sections.append({"type": "heading", "text": "Profit calculation"})
            split_label = ("Expenses divided equally among active partners"
                           if expense_split == "equal"
                           else "Expenses pro-rata by partner share %")
            sections.append({
                "type": "paragraph",
                "text": (
                    f"{split_label}. This partner's slice of total expenses "
                    f"for the selected range: {money_pkr(expense_deduction)}. "
                    f"Gross earnings {money_pkr(total_gross_pkr)} − expense "
                    f"deduction {money_pkr(expense_deduction)} = net earnings "
                    f"{money_pkr(net_pkr)}."
                ),
            })

        builder = PDFReportBuilder(
            title=f"Partner report — {partner.name}",
            subtitle=" · ".join(subtitle_parts),
            metadata=metadata,
        )
        pdf_bytes = builder.build(sections)

        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in partner.name
        ) or "partner"
        range_slug = (
            f"{date_from}_to_{date_to}" if (date_from or date_to) else "all-time"
        )
        filename = f"paybitnex-partner-{safe_name}-{profit_type}-{range_slug}.pdf"
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @action(
        detail=False, methods=["post"], url_path="shares/bulk",
        permission_classes=[IsAuthenticated, IsAdmin],
    )
    def bulk_update_shares(self, request):
        s = SharesBulkUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        results = []
        with dbtx.atomic():
            for item in s.validated_data["shares"]:
                pct = Decimal(str(item["percentage"]))
                share, _ = PartnerShare.objects.select_for_update().get_or_create(
                    partner_id=item["partner"],
                    defaults={"percentage": pct, "updated_by": request.user},
                )
                share.percentage = pct
                share.updated_by = request.user
                share.save(update_fields=["percentage", "updated_by", "updated_at"])
                results.append(PartnerShareSerializer(share).data)
            # AuditLog.after is a JSONField — Django's default JSON encoder
            # doesn't know how to serialize UUID objects, and DRF's
            # PrimaryKeyRelatedField returns raw UUIDs (not strings). So we
            # flatten to plain-JSON-compatible types before storing.
            json_safe_shares = [
                {k: str(v) if hasattr(v, "hex") else
                     str(v) if hasattr(v, "as_tuple") else v  # Decimal → str
                 for k, v in dict(row).items()}
                for row in results
            ]
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                description=f"Bulk share update: {len(results)} partners",
                after={"shares": json_safe_shares},
            )
        return Response({"updated": len(results), "shares": results})


class PartnerLedgerListView(viewsets.ReadOnlyModelViewSet):
    """All ledger entries across partners (admin / accountant)."""
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = (
        PartnerLedgerEntry.objects
        .select_related("partner", "payment", "payment__customer")
        .order_by("-created_at")
    )
    serializer_class = PartnerLedgerEntrySerializer
    filterset_fields = ["partner", "currency_code", "payment"]
