"""
Exchange rate fetching — runs every hour via Celery Beat.

Respects manual overrides: if a currency has manual_override_until set
and that datetime is in the future, we skip overwriting it.

Supported providers:
    - open-erapi          (https://open.er-api.com) — FREE, no key required.
    - exchangerate-api    (https://www.exchangerate-api.com) — requires API key.
    - exchangerate-host   (https://api.exchangerate.host) — free tier, no key.

Default provider is `open-erapi` and requires no credentials.

To add another provider, add a parser entry to `_PROVIDERS` below.
"""
import logging
from decimal import Decimal
from typing import Optional

import httpx
from celery import shared_task
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_open_erapi(data: dict, wanted: list[str]) -> dict[str, Decimal]:
    """
    open.er-api.com response:
        {
          "result": "success",
          "base_code": "PKR",
          "rates": {"USD": 0.0036, "EUR": 0.0033, ...}
        }
    The `rates` field tells us "1 base = X target". When we query with
    base=PKR, `rates["USD"]` is "1 PKR = 0.0036 USD" — invert to get
    "1 USD = 277.77 PKR".
    """
    if data.get("result") not in (None, "success"):
        log.warning("open-erapi returned non-success: %s", data.get("result"))
        return {}
    base = data.get("base_code") or data.get("base") or "PKR"
    rates = data.get("rates") or {}
    out: dict[str, Decimal] = {}
    for code in wanted:
        if code == base:
            out[code] = Decimal("1")
            continue
        r = rates.get(code)
        if r is None or r == 0:
            continue
        if base == "PKR":
            out[code] = (Decimal("1") / Decimal(str(r))).quantize(Decimal("0.000001"))
        else:
            out[code] = Decimal(str(r))
    return out


def _parse_exchangerate_api(data: dict, wanted: list[str]) -> dict[str, Decimal]:
    """
    v6.exchangerate-api.com response:
        {"base_code": "PKR", "conversion_rates": {"USD": 0.0036, ...}}
    """
    base = data.get("base_code") or data.get("base")
    rates = data.get("conversion_rates") or data.get("rates") or {}
    out: dict[str, Decimal] = {}
    for code in wanted:
        if code == base:
            out[code] = Decimal("1")
            continue
        r = rates.get(code)
        if r is None or r == 0:
            continue
        if base == "PKR":
            out[code] = (Decimal("1") / Decimal(str(r))).quantize(Decimal("0.000001"))
        else:
            out[code] = Decimal(str(r))
    return out


def _parse_exchangerate_host(data: dict, wanted: list[str]) -> dict[str, Decimal]:
    """
    api.exchangerate.host response:
        {"base": "PKR", "rates": {"USD": 0.0036, ...}}
    """
    return _parse_open_erapi(data, wanted)  # same shape


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS = {
    # Free, no credentials required. This is the default.
    "open-erapi": {
        "url": "https://open.er-api.com/v6/latest/PKR",
        "needs_key": False,
        "parser": _parse_open_erapi,
    },
    # Free tier, no credentials required.
    "exchangerate-host": {
        "url": "https://api.exchangerate.host/latest?base=PKR",
        "needs_key": False,
        "parser": _parse_exchangerate_host,
    },
    # Paid — requires EXCHANGE_RATE_API_KEY.
    "exchangerate-api": {
        "url": "https://v6.exchangerate-api.com/v6/{key}/latest/PKR",
        "needs_key": True,
        "parser": _parse_exchangerate_api,
    },
}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def _fetch_from_provider(wanted_codes: list[str]) -> Optional[dict[str, Decimal]]:
    provider_name = getattr(settings, "EXCHANGE_RATE_PROVIDER", "open-erapi")
    api_key = getattr(settings, "EXCHANGE_RATE_API_KEY", "") or ""

    provider = _PROVIDERS.get(provider_name)
    if not provider:
        log.warning("Unknown rate provider %r, falling back to open-erapi.", provider_name)
        provider = _PROVIDERS["open-erapi"]

    if provider["needs_key"] and not api_key:
        log.warning(
            "Provider %r requires an API key but none configured — "
            "falling back to open-erapi (credential-free).", provider_name,
        )
        provider = _PROVIDERS["open-erapi"]

    url = provider["url"].format(key=api_key) if provider["needs_key"] else provider["url"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        return provider["parser"](data, wanted_codes)
    except Exception as e:
        log.exception("Rate fetch failed (%s): %s", provider_name, e)
        return None


# ---------------------------------------------------------------------------
# Helpers callable from views (one-shot market quote, no DB writes)
# ---------------------------------------------------------------------------

async def fetch_market_quote(codes: list[str]) -> dict[str, Decimal]:
    """
    Return a best-effort dict of `{code: Decimal PKR_per_unit}` for the
    given currency codes, without touching the DB.

    Used by the Rates page to show today's live market rate as a reference
    even when our local ExchangeRate row is stale or manually overridden.
    """
    if not codes:
        return {}
    result = await _fetch_from_provider(codes)
    return result or {}


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_live_rates(self):
    """Sync wrapper for Celery. Delegates to async impl."""
    import asyncio
    try:
        return asyncio.run(_fetch_live_rates_async())
    except Exception as e:
        log.exception("fetch_live_rates failed")
        raise self.retry(exc=e)


async def _fetch_live_rates_async():
    from myapp.Models.Core_models import Currency
    from myapp.Models.Rate_models import ExchangeRate, ExchangeRateHistory

    codes = [
        c async for c in Currency.objects
        .filter(is_active=True, is_base=False)
        .values_list("code", flat=True)
    ]
    if not codes:
        log.info("No active non-base currencies configured.")
        return

    rates = await _fetch_from_provider(codes)
    if not rates:
        return

    now = timezone.now()
    updated = 0
    for code, rate_value in rates.items():
        try:
            current = await ExchangeRate.objects.aget(currency_id=code)
            if (
                current.source == ExchangeRate.SOURCE_MANUAL
                and current.manual_override_until
                and current.manual_override_until > now
            ):
                log.info("Skipping %s — manual override active.", code)
                continue
            current.rate_to_pkr = rate_value
            current.source = ExchangeRate.SOURCE_LIVE
            await current.asave(update_fields=["rate_to_pkr", "source", "updated_at"])
        except ExchangeRate.DoesNotExist:
            await ExchangeRate.objects.acreate(
                currency_id=code,
                rate_to_pkr=rate_value,
                source=ExchangeRate.SOURCE_LIVE,
            )

        await ExchangeRateHistory.objects.acreate(
            currency_code=code,
            rate_to_pkr=rate_value,
            source=ExchangeRate.SOURCE_LIVE,
        )
        updated += 1

    log.info("Exchange rates updated: %d currencies.", updated)
    return {"updated": updated}
