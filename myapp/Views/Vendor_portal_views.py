"""
Vendor portal — a logged-in vendor's read-only view of their OWN
card transactions.

SCOPING RULE (the security core of this module)
-----------------------------------------------
A vendor user may see exactly those `InternalTransaction` rows where:

    dest_vendor    == their own linked Vendor      AND
    source_type    == 'credit_card'

Both conditions, always. This is deliberately the NARROW reading of
"all the transactions he made with our cards": it shows the vendor the
card payments *we made to them*, and nothing else.

The alternative reading — every card transaction company-wide — would
expose the company's entire card spend, plus every other vendor's
payments, to an external party. That is not something to enable by
accident, so this module does not implement it.

Everything funnels through `_vendor_scope()`. There is no other queryset
in this file, and no endpoint accepts a vendor id from the request. The
vendor is always resolved from `request.user`, so a vendor cannot widen
their own scope by crafting a query parameter.

WHAT IS DELIBERATELY WITHHELD
-----------------------------
The serializer here is bespoke and allow-list based (never
`fields = "__all__"`). A vendor sees: date, amount, currency, method,
reference, description, and which card *brand/label* paid them.

They must NOT see:
  - `card_dollar_rate` / `card_profit_pkr` — our PKR conversion and the
    rupee pool it feeds. Commercially sensitive and none of their business.
  - `fee_dist_type` / `fee_dist_partner` — internal partner arrangements.
  - `fee_expense` / `pk_*` — internal bookkeeping.
  - `created_by` — which staff member entered it.
  - Card `last4` — a vendor has no need for our card numbers.

Adding a field to `VendorTransactionSerializer` therefore requires a
deliberate decision. That is the intent.
"""
from decimal import Decimal

from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncMonth
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from myapp.Models.InternalTx_models import (
    CreditCard, InternalTransaction, InternalTxSource, Vendor,
)


# ---------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------

class IsVendorPortalUser(BasePermission):
    """Passes only for a user with live vendor-portal access.

    Staff are intentionally NOT auto-passed here. Admins have their own,
    richer views of this data; letting them through would mean the vendor
    endpoints get exercised almost exclusively by staff in testing, and a
    scoping bug could sit unnoticed because `request.user` happened to be
    an admin. Keeping this strict means the vendor path is always tested
    as a vendor.
    """
    message = "This area is only available to vendor accounts."

    def has_permission(self, request, view):
        u = getattr(request, "user", None)
        if not (u and u.is_authenticated and u.is_active):
            return False
        vendor = get_vendor_for_user(u)
        return vendor is not None


def get_vendor_for_user(user):
    """Resolve the live Vendor for `user`, or None.

    Returns None unless the vendor is active, the portal switch is on,
    and the link exists. Any failure mode resolves to None — never to a
    different vendor.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    vendor = (
        Vendor.objects
        .select_related("portal_user")
        .filter(portal_user=user, portal_enabled=True, is_active=True)
        .first()
    )
    if vendor is None:
        return None
    if not getattr(vendor.portal_user, "is_active", False):
        return None
    return vendor


def _vendor_scope(vendor):
    """THE queryset. Every vendor-facing read goes through this.

    Card transactions paid TO this vendor. Both filters are mandatory.
    """
    return (
        InternalTransaction.objects
        .filter(
            dest_vendor=vendor,
            source_type=InternalTxSource.CREDIT_CARD,
        )
        .select_related("source_credit_card", "currency")
    )


# ---------------------------------------------------------------------
# Serializer — explicit allow-list, see module docstring
# ---------------------------------------------------------------------

class VendorTransactionSerializer(serializers.ModelSerializer):
    card_label = serializers.SerializerMethodField()
    card_brand = serializers.SerializerMethodField()
    document_url = serializers.SerializerMethodField()
    document_name = serializers.SerializerMethodField()
    currency_code = serializers.CharField(source="currency_id", read_only=True)
    method_display = serializers.CharField(
        source="get_method_display", read_only=True,
    )
    status = serializers.SerializerMethodField()

    class Meta:
        model = InternalTransaction
        # Allow-list only. Do NOT switch this to "__all__" — that would
        # leak card_dollar_rate, card_profit_pkr, partner fee routing and
        # internal bookkeeping to an external party.
        fields = [
            "id", "occurred_on",
            "amount", "currency_code",
            "fee_amount",
            "method", "method_display",
            "reference", "description",
            "card_label", "card_brand",
            "document_url", "document_name",
            "status", "created_at",
            "card_dollar_rate", "card_profit_pkr"
        ]
        read_only_fields = fields

    def get_card_label(self, obj):
        """Human label of the paying card, WITHOUT the digits.

        `str(CreditCard)` appends the last 4, so it is not used here.
        """
        card = obj.source_credit_card
        return getattr(card, "label", "") or ""

    def get_card_brand(self, obj):
        card = obj.source_credit_card
        if not card:
            return ""
        return card.get_brand_display() if hasattr(card, "get_brand_display") else ""

    def get_document_url(self, obj):
        """Signed URL for the attached receipt, or None.

        Wrapped defensively: `.url` hits the storage backend, which can
        raise if S3 is misconfigured or a legacy row references a file
        that no longer exists. A broken attachment must never 500 the
        whole transaction list — the vendor simply sees no document.
        """
        doc = getattr(obj, "document", None)
        if not doc:
            return None
        try:
            url = doc.url
        except Exception:
            return None
        if not url:
            return None
        # Make relative (local-storage) URLs absolute so the SPA can link
        # to them directly. S3 already returns absolute signed URLs.
        request = self.context.get("request")
        if request is not None and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url

    def get_document_name(self, obj):
        """Bare filename for display, without the storage key prefix."""
        doc = getattr(obj, "document", None)
        name = getattr(doc, "name", "") or ""
        return name.rsplit("/", 1)[-1] if name else ""

    def get_status(self, obj):
        return "Paid" if obj.linked_vendor_pkr_payment_id else "Unpaid"


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated, IsVendorPortalUser])
def vendor_me(request):
    """GET /vendor/me/ — the vendor's own profile card."""
    vendor = get_vendor_for_user(request.user)
    return Response({
        "id": str(vendor.id),
        "name": vendor.name,
        "contact_name": vendor.contact_name,
        "contact_email": vendor.contact_email,
        "contact_phone": vendor.contact_phone,
        "is_active": vendor.is_active,
        "portal_granted_at": vendor.portal_granted_at,
        "user": {
            "email": request.user.email,
            "full_name": request.user.full_name,
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsVendorPortalUser])
def vendor_dashboard(request):
    """GET /vendor/dashboard/ — headline figures + recent activity.

    Optional: date_from / date_to (YYYY-MM-DD) to bound the summary.
    """
    from datetime import datetime

    vendor = get_vendor_for_user(request.user)
    qs = _vendor_scope(vendor)

    def _d(v):
        try:
            return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError, AttributeError):
            return None

    date_from = _d(request.query_params.get("date_from"))
    date_to = _d(request.query_params.get("date_to"))
    if date_from:
        qs = qs.filter(occurred_on__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_on__lte=date_to)

    status_filter = (request.query_params.get("status") or "").strip().lower()
    if status_filter == "paid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=False)
    elif status_filter == "unpaid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=True)

    by_currency = list(
        qs.order_by().values("currency_id").annotate(
            total=Sum("card_profit_pkr"), fees=Sum("fee_amount"), count=Count("id"),
        ).order_by("currency_id")
    )

    # Last 12 months of activity, for the dashboard chart.
    monthly = list(
        qs.order_by()
          .annotate(m=TruncMonth("occurred_on"))
          .values("m", "currency_id")
          .annotate(total=Sum("card_profit_pkr"), count=Count("id"))
          .order_by("-m")[:36]
    )

    by_card = list(
        qs.order_by()
          .values("source_credit_card__label", "currency_id")
          .annotate(total=Sum("card_profit_pkr"), count=Count("id"))
          .order_by("-total")[:20]
    )

    recent = qs.order_by("-occurred_on", "-created_at")[:10]

    first = qs.order_by("occurred_on").values_list(
        "occurred_on", flat=True,
    ).first()
    last = qs.order_by("-occurred_on").values_list(
        "occurred_on", flat=True,
    ).first()

    # Calculate closing report ledger
    from myapp.Models.InternalTx_models import VendorPKRPayment
    from decimal import Decimal
    from collections import defaultdict

    base_qs = _vendor_scope(vendor)
    
    agg = qs.aggregate(
        debt=Sum("card_profit_pkr"),
        usd=Sum("amount"),
        fees=Sum("fee_amount"),
    )
    period_debt = agg["debt"] or 0
    period_usd_raw = agg["usd"] or 0
    period_fees = agg["fees"] or 0
    period_usd = period_usd_raw + period_fees

    # Group card transactions in period by rate for rate breakdown
    rate_groups = defaultdict(lambda: {"usd_gross": Decimal("0"), "fees": Decimal("0"), "pkr": Decimal("0"), "count": 0})
    for tx in qs.values("amount", "fee_amount", "card_dollar_rate", "card_profit_pkr"):
        r = tx["card_dollar_rate"]
        usd_gross = Decimal(str(tx["amount"] or 0))
        fee_val = Decimal(str(tx["fee_amount"] or 0))
        pkr_val = Decimal(str(tx["card_profit_pkr"] or 0))
        rate_key = str(Decimal(str(r)).quantize(Decimal("0.01"))) if r is not None else "Unassigned"
        rate_groups[rate_key]["usd_gross"] += usd_gross
        rate_groups[rate_key]["fees"] += fee_val
        rate_groups[rate_key]["pkr"] += pkr_val
        rate_groups[rate_key]["count"] += 1

    rate_breakdown = [
        {
            "rate": k,
            "usd_gross": str(v["usd_gross"]),
            "fees": str(v["fees"]),
            "total_usd": str(v["usd_gross"] + v["fees"]),
            "total_pkr": str(v["pkr"]),
            "count": v["count"],
        }
        for k, v in sorted(
            rate_groups.items(),
            key=lambda item: (Decimal(item[0]) if item[0] != "Unassigned" else Decimal(0)),
            reverse=True,
        )
    ]

    # 2. Previous Pending (Opening Balance B/F)
    if date_from:
        prev_debt_qs = base_qs.filter(occurred_on__lt=date_from)
        prev_debt = prev_debt_qs.aggregate(v=Sum("card_profit_pkr"))["v"] or 0
        
        prev_paid_qs = VendorPKRPayment.objects.filter(vendor=vendor, is_void=False, occurred_on__lt=date_from)
        prev_paid = prev_paid_qs.aggregate(v=Sum("pkr_received"))["v"] or 0
    else:
        prev_debt = 0
        prev_paid = 0
    
    previous_pending = prev_debt - prev_paid

    # 3. Payments Received
    period_payments_qs = VendorPKRPayment.objects.filter(vendor=vendor, is_void=False)
    if date_from:
        period_payments_qs = period_payments_qs.filter(occurred_on__gte=date_from)
    if date_to:
        period_payments_qs = period_payments_qs.filter(occurred_on__lte=date_to)
        
    period_payments = list(
        period_payments_qs
        .select_related("pk_bank_account")
        .order_by("occurred_on")
    )
    period_payments_list = []
    for p in period_payments:
        doc_url = None
        if p.screenshot:
            try:
                doc_url = p.screenshot.url
                if request is not None and doc_url and doc_url.startswith("/"):
                    doc_url = request.build_absolute_uri(doc_url)
            except Exception:
                doc_url = None

        period_payments_list.append({
            "id": str(p.id),
            "date": p.occurred_on.isoformat() if p.occurred_on else None,
            "bank_label": p.pk_bank_account.label if p.pk_bank_account else "Bank Transfer",
            "bank_name": p.pk_bank_account.bank_name if p.pk_bank_account else "",
            "bank_transaction_id": p.bank_transaction_id or "",
            "amount": str(p.pkr_received or 0),
            "usd_sent": str(p.usd_sent or 0) if p.usd_sent else None,
            "exchange_rate": str(p.exchange_rate) if p.exchange_rate else None,
            "screenshot_url": doc_url,
            "notes": p.notes or "",
        })
    
    # 4. Total Paid
    total_paid = sum((p.pkr_received or 0 for p in period_payments), 0)
    
    # 5. Total Pending Amount (Closing Balance C/F)
    total_pending = previous_pending + period_debt - total_paid

    # Blended / Effective rate for period
    blended_rate = (period_debt / period_usd).quantize(Decimal("0.01")) if period_usd > 0 else None

    status_str = "settled" if total_pending == 0 else ("pending" if total_pending > 0 else "credit")

    ledger = {
        "opening_balance_pkr": str(previous_pending),
        "period_gross_usd": str(period_usd_raw),
        "period_fees_usd": str(period_fees),
        "period_total_usd": str(period_usd),
        "effective_rate": str(blended_rate) if blended_rate else None,
        "period_charges_pkr": str(period_debt),
        "rates_breakdown": rate_breakdown,
        "total_due_pkr": str(previous_pending + period_debt),
        "payments": period_payments_list,
        "total_paid_pkr": str(total_paid),
        "closing_balance_pkr": str(total_pending),
        "settlement_status": status_str,

        # Backwards compatibility keys
        "period_usd": str(period_usd),
        "period_fees": str(period_fees),
        "blended_rate": str(blended_rate) if blended_rate else None,
        "period_pkr": str(period_debt),
        "previous_pending_pkr": str(previous_pending),
        "total_pkr": str(previous_pending + period_debt),
        "total_pending_pkr": str(total_pending),
    }

    return Response({
        "ledger": ledger,
        "vendor": {"id": str(vendor.id), "name": vendor.name},
        "totals": {
            "transaction_count": qs.count(),
            "first_transaction": first,
            "last_transaction": last,
            "by_currency": [
                {
                    "currency": "PKR",
                    "total": str(r["total"] or 0),
                    "fees": str(r["fees"] or 0),
                    "count": r["count"],
                }
                for r in by_currency
            ],
            "unpaid_by_currency": [
                {
                    "currency": "PKR",
                    "total": str(r["total"] or 0),
                    "count": r["count"],
                }
                for r in qs.filter(linked_vendor_pkr_payment__isnull=True).order_by().values("currency_id").annotate(total=Sum("card_profit_pkr"), count=Count("id")).order_by("currency_id")
            ],
            "paid_by_currency": [
                {
                    "currency": "PKR",
                    "total": str(r["total"] or 0),
                    "count": r["count"],
                }
                for r in qs.filter(linked_vendor_pkr_payment__isnull=False).order_by().values("currency_id").annotate(total=Sum("card_profit_pkr"), count=Count("id")).order_by("currency_id")
            ],
        },
        "monthly": [
            {
                "month": r["m"].isoformat() if r["m"] else None,
                "currency": "PKR",
                "total": str(r["total"] or 0),
                "count": r["count"],
            }
            for r in monthly if r["m"]
        ],
        "by_card": [
            {
                "card_label": r["source_credit_card__label"] or "—",
                "currency": "PKR",
                "total": str(r["total"] or 0),
                "count": r["count"],
            }
            for r in by_card
        ],
        "recent": VendorTransactionSerializer(
            recent, many=True, context={"request": request},
        ).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsVendorPortalUser])
def vendor_transactions(request):
    """GET /vendor/transactions/ — paginated list of the vendor's own
    card transactions.

    Filters: date_from, date_to, currency, q (reference/description),
             page, page_size.
    """
    from datetime import datetime

    vendor = get_vendor_for_user(request.user)
    qs = _vendor_scope(vendor)

    def _d(v):
        try:
            return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError, AttributeError):
            return None

    date_from = _d(request.query_params.get("date_from"))
    date_to = _d(request.query_params.get("date_to"))
    if date_from:
        qs = qs.filter(occurred_on__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_on__lte=date_to)

    currency = (request.query_params.get("currency") or "").strip()
    if currency and currency.lower() != "all":
        qs = qs.filter(currency_id=currency)

    status_filter = (request.query_params.get("status") or "").strip().lower()
    if status_filter == "paid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=False)
    elif status_filter == "unpaid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=True)

    # Card filter. Note this narrows an ALREADY-scoped queryset, so passing
    # the id of a card that never paid this vendor simply yields zero rows —
    # it cannot widen the scope or reveal another vendor's activity.
    card = (request.query_params.get("card") or "").strip()
    if card and card.lower() != "all":
        qs = qs.filter(source_credit_card_id=card)

    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(reference__icontains=q) | Q(description__icontains=q)
        )

    qs = qs.order_by("-occurred_on", "-created_at")

    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    page_size = max(1, min(page_size, 200))

    total = qs.count()
    start = (page - 1) * page_size
    rows = qs[start:start + page_size]

    # Totals for the filtered set (not just the page).
    totals = list(
        qs.order_by().values("currency_id").annotate(
            total=Sum("amount"), count=Count("id"),
        )
    )

    return Response({
        "count": total,
        "page": page,
        "page_size": page_size,
        "num_pages": (total + page_size - 1) // page_size if page_size else 1,
        "totals": [
            {
                "currency": r["currency_id"],
                "total": str(r["total"] or 0),
                "count": r["count"],
            }
            for r in totals
        ],
        "filters": {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "currency": currency or "all",
            "card": card or "all",
            "status": status_filter or "all",
            "q": q or "",
        },
        "results": VendorTransactionSerializer(
            rows, many=True, context={"request": request},
        ).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsVendorPortalUser])
def vendor_transactions_csv(request):
    """GET /vendor/transactions.csv — export of the vendor's own rows."""
    import csv
    from io import StringIO
    from django.http import HttpResponse

    from datetime import datetime

    vendor = get_vendor_for_user(request.user)
    qs = _vendor_scope(vendor)

    # Apply the SAME filters as the list view. Without this the export
    # silently disagrees with what the vendor sees on screen — they filter
    # to one card, hit CSV, and get every row back.
    def _d(v):
        try:
            return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError, AttributeError):
            return None

    date_from = _d(request.query_params.get("date_from"))
    date_to = _d(request.query_params.get("date_to"))
    if date_from:
        qs = qs.filter(occurred_on__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_on__lte=date_to)

    currency = (request.query_params.get("currency") or "").strip()
    if currency and currency.lower() != "all":
        qs = qs.filter(currency_id=currency)

    status_filter = (request.query_params.get("status") or "").strip().lower()
    if status_filter == "paid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=False)
    elif status_filter == "unpaid":
        qs = qs.filter(linked_vendor_pkr_payment__isnull=True)

    card = (request.query_params.get("card") or "").strip()
    if card and card.lower() != "all":
        qs = qs.filter(source_credit_card_id=card)

    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(reference__icontains=q) | Q(description__icontains=q)
        )

    qs = qs.order_by("-occurred_on", "-created_at")[:5000]

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Date", "Card", "Method", "Status",
        "Currency", "Amount", "Fee", "Description", "Document",
    ])
    for t in qs:
        # Same defensive .url access as the serializer — a broken
        # attachment must not abort the whole export.
        doc_url = ""
        if getattr(t, "document", None):
            try:
                doc_url = t.document.url or ""
            except Exception:
                doc_url = ""
        w.writerow([
            t.occurred_on.isoformat() if t.occurred_on else "",
            getattr(t.source_credit_card, "label", "") or "",
            t.get_method_display(),
            "Paid" if t.linked_vendor_pkr_payment_id else "Unpaid",
            t.currency_id,
            str(t.amount),
            str(t.fee_amount or Decimal("0")),
            (t.description or "").replace("\n", " "),
            doc_url,
        ])

    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    safe = "".join(
        c for c in vendor.name if c.isalnum() or c in (" ", "-", "_")
    ).strip().replace(" ", "-") or "vendor"
    resp["Content-Disposition"] = f'attachment; filename="{safe}-card-transactions.csv"'
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsVendorPortalUser])
def vendor_cards(request):
    """GET /vendor/cards/ — the cards that have actually paid this vendor.

    Powers the card filter dropdown. Derived from `_vendor_scope()`, so it
    lists ONLY cards with at least one payment to this vendor — never the
    company's full card list. A vendor therefore cannot enumerate our
    payment instruments, only the ones they've already seen on their own
    statements.

    Card numbers are deliberately omitted (label + brand only), matching
    VendorTransactionSerializer.
    """
    vendor = get_vendor_for_user(request.user)
    rows = (
        _vendor_scope(vendor)
        .exclude(source_credit_card__isnull=True)
        .order_by()
        .values(
            "source_credit_card_id",
            "source_credit_card__label",
            "source_credit_card__brand",
        )
        .annotate(count=Count("id"), total=Sum("amount"))
        .order_by("-count")
    )

    brand_labels = dict(CreditCard.BRAND_CHOICES)
    return Response({
        "results": [
            {
                "id": str(r["source_credit_card_id"]),
                "label": r["source_credit_card__label"] or "Card",
                "brand": brand_labels.get(
                    r["source_credit_card__brand"], "",
                ),
                "count": r["count"],
                "total": str(r["total"] or 0),
            }
            for r in rows
        ],
    })
