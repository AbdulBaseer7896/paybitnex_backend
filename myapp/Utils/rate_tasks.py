"""
Exchange rate fetching — runs every hour via Celery Beat.

**Multi-provider fallback chain.** We try the first provider; if it fails
(network error, non-200, missing rate), we move on to the next. This makes
the rate pipeline robust against any single provider going down.

Providers, in order:
    1. jsdelivr-fawazahmed       (https://cdn.jsdelivr.net/gh/fawazahmed0/currency-api)
    2. hexarate                  (https://hexarate.paikama.co)
    3. moneyconvert              (https://cdn.moneyconvert.net)
    4. open-erapi                (https://open.er-api.com) — original fallback

No provider in the list requires an API key.

Respects manual overrides: if a currency has manual_override_until set
and that datetime is in the future, we skip overwriting it.
"""
import asyncio
import logging
from decimal import Decimal
from typing import Optional

import httpx
from celery import shared_task
from django.utils import timezone

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-provider single-pair fetchers.
# Each returns Decimal PKR-per-unit, or None on any failure.
# ---------------------------------------------------------------------------

async def _try_jsdelivr_fawazahmed(client: httpx.AsyncClient, code: str) -> Optional[Decimal]:
    """
    https://cdn.jsdelivr.net/gh/fawazahmed0/currency-api@1/latest/currencies/usd/pkr.json
    Response: {"date": "...", "pkr": 278.23}
    """
    lc = code.lower()
    url = f"https://cdn.jsdelivr.net/gh/fawazahmed0/currency-api@1/latest/currencies/{lc}/pkr.json"
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        val = data.get("pkr")
        if val is None or val == 0:
            return None
        return Decimal(str(val)).quantize(Decimal("0.000001"))
    except Exception as e:
        log.info("jsdelivr-fawazahmed failed for %s: %s", code, e)
        return None


async def _try_hexarate(client: httpx.AsyncClient, code: str) -> Optional[Decimal]:
    """
    https://hexarate.paikama.co/api/rates/USD/PKR/latest
    Response: {"status_code": 200, "data": {"mid": 278.35, ...}}
    """
    url = f"https://hexarate.paikama.co/api/rates/{code.upper()}/PKR/latest"
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        rates = data.get("data") or {}
        val = rates.get("mid") or rates.get("rate") or rates.get("value")
        if val is None or val == 0:
            return None
        return Decimal(str(val)).quantize(Decimal("0.000001"))
    except Exception as e:
        log.info("hexarate failed for %s: %s", code, e)
        return None


async def _try_moneyconvert(client: httpx.AsyncClient, code: str) -> Optional[Decimal]:
    """
    https://cdn.moneyconvert.net/api/latest.json
    Response: {"base": "USD", "rates": {"PKR": 278.12, "EUR": 0.92, ...}}
    We request this endpoint once per currency — API is keyed on 'base'.
    """
    url = f"https://cdn.moneyconvert.net/api/latest.json?base={code.upper()}"
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates") or {}
        val = rates.get("PKR")
        if val is None or val == 0:
            # Some mirrors only publish USD base — derive via USD when possible
            if code.upper() != "USD":
                return None
            return None
        return Decimal(str(val)).quantize(Decimal("0.000001"))
    except Exception as e:
        log.info("moneyconvert failed for %s: %s", code, e)
        return None


async def _try_open_erapi(client: httpx.AsyncClient, code: str) -> Optional[Decimal]:
    """
    https://open.er-api.com/v6/latest/USD → {"rates": {"PKR": 278.1, ...}}
    """
    url = f"https://open.er-api.com/v6/latest/{code.upper()}"
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        if data.get("result") not in (None, "success"):
            return None
        rates = data.get("rates") or {}
        val = rates.get("PKR")
        if val is None or val == 0:
            return None
        return Decimal(str(val)).quantize(Decimal("0.000001"))
    except Exception as e:
        log.info("open-erapi failed for %s: %s", code, e)
        return None


# Ordered chain — top = tried first
_PROVIDER_CHAIN = [
    ("jsdelivr-fawazahmed", _try_jsdelivr_fawazahmed),
    ("hexarate",            _try_hexarate),
    ("moneyconvert",        _try_moneyconvert),
    ("open-erapi",          _try_open_erapi),
]


async def _fetch_one_with_fallback(client: httpx.AsyncClient, code: str) -> Optional[tuple[Decimal, str]]:
    """
    Try each provider in order for a single currency. Return (rate, provider_name)
    on first success, or None if ALL providers fail.
    """
    for name, fn in _PROVIDER_CHAIN:
        rate = await fn(client, code)
        if rate is not None and rate > 0:
            return (rate, name)
    return None


async def _fetch_from_provider(wanted_codes: list[str]) -> Optional[dict[str, Decimal]]:
    """
    Fetch every wanted code, using the provider chain per-currency.
    A single currency failing on every provider is logged but doesn't
    prevent the others from being returned.
    """
    if not wanted_codes:
        return {}
    out: dict[str, Decimal] = {}
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        remote_codes = [code for code in wanted_codes if code != "PKR"]
        results = await asyncio.gather(*(
            _fetch_one_with_fallback(client, code) for code in remote_codes
        ))
        if "PKR" in wanted_codes:
            out["PKR"] = Decimal("1")
        for code, result in zip(remote_codes, results):
            if result is None:
                log.warning("All providers failed for currency %s", code)
                continue
            rate, source = result
            out[code] = rate
            log.info("Rate for %s = %s PKR (via %s)", code, rate, source)
    return out or None


# ---------------------------------------------------------------------------
# Helpers callable from views (one-shot market quote, no DB writes)
# ---------------------------------------------------------------------------

async def fetch_market_quote(codes: list[str]) -> dict[str, Decimal]:
    """
    Return a best-effort dict of `{code: Decimal PKR_per_unit}` for the
    given currency codes, without touching the DB.
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
                # Keep a fresh market reference while leaving the manually
                # overridden operational rate untouched.
                await ExchangeRateHistory.objects.acreate(
                    currency_code=code,
                    rate_to_pkr=rate_value,
                    source=ExchangeRate.SOURCE_LIVE,
                )
                updated += 1
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
