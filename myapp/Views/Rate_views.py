"""
Rate views:
- GET  /rates/                   → list current rates (any authenticated user).
                                   If DB is empty, pulls from the credential-free
                                   public API once and caches in the DB.
- GET  /rates/live/              → fetch today's market quote from the public
                                   API WITHOUT touching the DB. Handy for the
                                   Rates page's "reference rate" chip.
- GET  /rates/history/           → rate change history (staff)
- POST /rates/override/          → manual override (admin/accountant)
- POST /rates/refresh/           → trigger an immediate live fetch (admin)
"""
import asyncio
import logging
from datetime import timedelta
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.generics import ListAPIView
from rest_framework.views import APIView

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Core_models import Currency
from myapp.Models.Rate_models import ExchangeRate, ExchangeRateHistory
from myapp.serializers.Rate_serializers import (
    ExchangeRateSerializer, ExchangeRateHistorySerializer,
    ManualRateOverrideSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.rate_tasks import fetch_market_quote

log = logging.getLogger(__name__)


class ExchangeRateListView(APIView):
    """
    Lists the current ExchangeRate rows. If the table is empty we seed it
    from the free public API so the UI never shows a blank state on first use.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rates_qs = (
            ExchangeRate.objects
            .select_related("currency")
            .all()
        )
        # Seed from public API if we have none yet — one-time warm-up.
        if not rates_qs.exists():
            self._seed_from_public_api()
            rates_qs = (
                ExchangeRate.objects
                .select_related("currency")
                .all()
            )

        data = ExchangeRateSerializer(rates_qs, many=True).data
        return Response(data)

    @staticmethod
    def _seed_from_public_api():
        codes = list(
            Currency.objects
            .filter(is_active=True, is_base=False)
            .values_list("code", flat=True)
        )
        if not codes:
            return
        quotes = asyncio.run(fetch_market_quote(codes))
        if not quotes:
            return
        for code, val in quotes.items():
            ExchangeRate.objects.update_or_create(
                currency_id=code,
                defaults={
                    "rate_to_pkr": val,
                    "source": ExchangeRate.SOURCE_LIVE,
                },
            )
            ExchangeRateHistory.objects.create(
                currency_code=code, rate_to_pkr=val,
                source=ExchangeRate.SOURCE_LIVE,
            )


class LiveMarketQuoteView(APIView):
    """
    Returns the latest market quote persisted by the hourly background task.
    Page loads never wait on a third-party exchange-rate provider.

    Query: ?codes=USD,EUR,GBP   (optional — defaults to all active non-base)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        codes_param = request.query_params.get("codes", "").strip()
        if codes_param:
            codes = [c.strip().upper() for c in codes_param.split(",") if c.strip()]
        else:
            codes = list(
                Currency.objects
                .filter(is_active=True, is_base=False)
                .values_list("code", flat=True)
            )
        if not codes:
            return Response([], status=status.HTTP_200_OK)

        latest_live = ExchangeRateHistory.objects.filter(
            currency_code=OuterRef("code"),
            source=ExchangeRate.SOURCE_LIVE,
        ).order_by("-created_at")
        stored = {
            row["code"]: row
            for row in Currency.objects.filter(code__in=codes).annotate(
                market_rate=Subquery(latest_live.values("rate_to_pkr")[:1]),
                market_fetched_at=Subquery(latest_live.values("created_at")[:1]),
            ).values("code", "market_rate", "market_fetched_at")
        }
        rows = [{
            "currency_code": code,
            "rate_to_pkr": (
                str(stored[code]["market_rate"])
                if code in stored and stored[code]["market_rate"] is not None
                else None
            ),
            "source": "stored-live-api",
        } for code in codes]
        fetched_times = [
            row["market_fetched_at"] for row in stored.values()
            if row["market_fetched_at"] is not None
        ]
        return Response({
            "fetched_at": max(fetched_times).isoformat() if fetched_times else None,
            "results": rows,
        })


class ExchangeRateHistoryView(ListAPIView):
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = ExchangeRateHistory.objects.all()
    serializer_class = ExchangeRateHistorySerializer
    filterset_fields = ["currency_code", "source"]
    ordering = ["-created_at"]


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def manual_override(request):
    s = ManualRateOverrideSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    code = s.validated_data["currency"]
    rate_value = s.validated_data["rate_to_pkr"]
    hours = s.validated_data.get("override_hours", 24)

    try:
        rate = ExchangeRate.objects.get(currency_id=code)
        before = {
            "rate_to_pkr": str(rate.rate_to_pkr),
            "source": rate.source,
        }
    except ExchangeRate.DoesNotExist:
        rate = ExchangeRate(currency_id=code)
        before = None

    rate.rate_to_pkr = rate_value
    rate.source = ExchangeRate.SOURCE_MANUAL
    rate.manual_override_until = timezone.now() + timedelta(hours=hours)
    rate.updated_by = request.user
    rate.save()

    ExchangeRateHistory.objects.create(
        currency_code=code,
        rate_to_pkr=rate_value,
        source=ExchangeRate.SOURCE_MANUAL,
        set_by=request.user,
    )
    AuditLog.record(
        user=request.user, action=AuditLog.ACTION_RATE_CHANGE, target=rate,
        description=f"Manual override {code} = {rate_value} PKR for {hours}h",
        before=before, after={"rate_to_pkr": str(rate_value), "source": "manual"},
    )
    return Response(ExchangeRateSerializer(rate).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def trigger_refresh(request):
    """Queue an immediate rate fetch without blocking the HTTP request."""
    from myapp.Utils.rate_tasks import fetch_live_rates

    refresh_task = fetch_live_rates
    delay_method = getattr(refresh_task, "delay", None)
    if not callable(delay_method):
        log.error("Live rate refresh task is not callable: %r", refresh_task)
        return Response(
            {"detail": "Rate refresh service is unavailable."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        delay_method()
    except Exception:
        log.exception("Could not queue exchange-rate refresh")
        return Response(
            {"detail": "Rate refresh could not be queued. Please try again."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({"detail": "Rate refresh queued."})
