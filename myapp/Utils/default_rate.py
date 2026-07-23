"""
Default dollar-rate resolution for newly-created transactions.

WHY THIS EXISTS
---------------
Historically a transaction carried no `exchange_rate` until an accountant
verified it and typed one in. That meant any report which multiplies by the
rate (gross_pkr / net_pkr) produced NULLs for un-processed rows, so the
weekly report looked empty until every transaction had been touched by hand.

Now every payment gets a *provisional* rate the moment it is created, so
reports can show a meaningful PKR figure immediately. The accountant still
overwrites it with the real, negotiated rate during processing — this is
only a placeholder to keep reporting non-empty.

RESOLUTION ORDER
----------------
1. `SystemSetting["default_dollar_rate"]`   — explicit admin override.
      Set this if you want a fixed, predictable placeholder that does not
      drift with the live feed. Leave it unset to use (2).
2. `ExchangeRate.rate_to_pkr` for the currency — the live/manual rate the
      hourly Celery task maintains. This is the default behaviour.
3. `None` — no rate could be resolved. The payment is created without one,
      exactly as before this change. Nothing breaks; the row simply shows
      no PKR value until processed.

PROVISIONAL vs FINAL
--------------------
`IncomingPayment.is_rate_provisional` records whether the rate currently on
the row came from here (True) or from a human (False). Reports surface this
so nobody closes a week believing placeholder numbers are final.

IMPORTANT: applying a provisional rate deliberately does NOT compute
fee/net amounts. `calculate_amounts()` needs `fee_percentage`, which is a
commercial decision made during processing. We only populate
`exchange_rate` and the reference-only `gross_pkr`. Fee-dependent columns
(`net_pkr`, `fee_amount_foreign`, `net_amount_foreign`) stay NULL until an
accountant applies the real rate and fee, so no partner ledger or profit
figure is ever derived from a placeholder.
"""
from decimal import Decimal, InvalidOperation

SETTING_KEY = "default_dollar_rate"


def _clean(value):
    """Coerce a stored string to a positive Decimal, or None."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return dec if dec > 0 else None


def get_default_rate(currency_code="USD"):
    """Return the provisional rate for `currency_code`, or None.

    Never raises — a failure to resolve a rate must not block a customer
    from submitting a payment.
    """
    from myapp.Models.Core_models import SystemSetting

    # (1) Explicit admin override wins.
    override = _clean(SystemSetting.get(SETTING_KEY, None))
    if override is not None:
        return override

    # (2) Fall back to the maintained live/manual rate for this currency.
    try:
        from myapp.Models.Rate_models import ExchangeRate
        row = ExchangeRate.objects.filter(currency_id=currency_code).first()
        if row is not None:
            return _clean(row.rate_to_pkr)
    except Exception:
        # Table missing during an early migration, DB hiccup, etc.
        pass

    # (3) Nothing available.
    return None


def apply_default_rate(payment):
    """Stamp a provisional rate onto an unsaved/just-created payment.

    Mutates `payment` in place and returns True if a rate was applied.
    Does nothing when the payment already carries a rate (so this is safe
    to call from multiple creation paths, and safe to re-run).
    """
    if payment.exchange_rate is not None:
        return False

    currency_code = getattr(payment.currency, "code", None) or payment.currency_id
    rate = get_default_rate(currency_code)
    if rate is None:
        return False

    payment.exchange_rate = rate
    payment.is_rate_provisional = True

    # Reference-only gross figure. Intentionally NOT calling
    # calculate_amounts() — see the module docstring.
    try:
        payment.gross_pkr = (Decimal(str(payment.amount)) * rate).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, TypeError):
        payment.gross_pkr = None

    return True
