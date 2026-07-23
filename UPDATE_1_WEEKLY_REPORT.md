# Update 1 — Weekly report fix + Monday-to-Monday report

## The problem

The weekly report was blank until each transaction had been manually
verified and given a dollar rate, because:

1. `exchange_rate` was `NULL` until an accountant typed it in, so every
   PKR column (`gross_pkr`, `net_pkr`) was `NULL` too.
2. `/reports/closing/` defaults to `status=completed`, so anything not
   fully processed was filtered out before it could be counted.

## The fix — two independent parts

### Part A — every transaction now gets a dollar rate at creation

A **provisional** rate is stamped on each payment the moment it is
created. The accountant still overwrites it with the real rate during
processing; this is only a placeholder so reports are never empty.

Resolution order (`myapp/Utils/default_rate.py`):

1. `SystemSetting["default_dollar_rate"]` — a fixed admin override.
2. `ExchangeRate.rate_to_pkr` for the currency — the live rate the
   existing hourly Celery task already maintains. **This is the default.**
3. Nothing → no rate applied, exactly as before. Never blocks a submission.

**To use a fixed rate instead of the live one**, set
`default_dollar_rate` to e.g. `280` in Settings → System Settings.
Leave it blank to track the live rate.

#### Safety property — placeholders never touch money

Applying a provisional rate sets **only** `exchange_rate` and the
reference-only `gross_pkr`. It deliberately does **not** call
`calculate_amounts()`, so `net_pkr`, `fee_amount_foreign` and
`net_amount_foreign` stay `NULL` until a human applies the real rate
and fee.

This means a placeholder rate can never flow into a profit figure, a
partner ledger entry, or a customer payout amount. Fee percentage is a
commercial decision and is never guessed.

`is_rate_provisional` (new boolean) records whether the current rate is
a placeholder. It is set `False` the instant the accountant applies a
real rate via the existing accountant-apply endpoint.

### Part B — new Monday-to-Monday report

New endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/reports/weekly-monday/` | Week-by-week summary |
| `GET /api/v1/reports/weekly-monday/detail/?week_start=YYYY-MM-DD` | Line items for one week |

Behaviour:

- **Includes every status except `rejected`** — rejected money never
  existed, so counting it would inflate the week. Asking for
  `status=rejected` returns a `400` rather than silently returning
  nothing.
- **Buckets by `occurred_on`**, falling back to `created_at` for legacy
  rows. This matters: a Friday batch entered on Monday lands in
  **Friday's** week. (The older `/reports/closing/` still buckets on
  `created_at`; that was left untouched so existing reports don't shift.)
- **Weeks are inclusive-start, exclusive-end.** Mon 5 Jan covers 5–11 Jan;
  Mon 12 Jan starts the next week. Verified against a full 365-day sweep:
  no gaps, no overlaps, every interior week exactly 7 days.
- **Honours `bitnex_week_start_day`** (default `0` = Monday), so if the
  company ever moves its week the report follows.
- Weeks with zero transactions still appear, as explicit zero rows.
- **No fee/profit figures**, by design — see the safety property above.

## Frontend

Reports page now has a **"Monday to Monday report"** button in the header
(and a matching tab). It shows:

- Per-week totals with a per-status chip breakdown.
- Click any week to expand a line-item table.
- An amber banner and per-row `EST` marker wherever a placeholder rate is
  still in use, so a week is never closed on estimated numbers.

## Files changed

**Backend**
```
myapp/Utils/default_rate.py                        NEW
myapp/Views/Weekly_report_views.py                 NEW
myapp/migrations/0052_..._is_rate_provisional.py   NEW
myapp/Models/Transaction_models.py                 + is_rate_provisional
myapp/Views/Transaction_views.py                   + stamp rate on both create paths
                                                   + clear flag on accountant apply
myapp/Urls/Report_urls.py                          + 2 routes
myapp/serializers/Transaction_serializers.py       + expose is_rate_provisional
myapp/management/commands/seed_initial_data.py     + default_dollar_rate setting
```

**Frontend**
```
src/pages/admin/MondayWeeklyReport.jsx             NEW
src/pages/admin/ReportsPage.jsx                    + button, tab, render
```

## Migration

`0052` adds the boolean **and backfills historical rows** that have no
rate, so old transactions appear in the new report immediately. The
backfill:

- never touches rows that already have a rate;
- skips rejected rows;
- sets only `exchange_rate` + `gross_pkr` (never fee/net columns);
- does nothing at all if no rate can be resolved;
- is fully reversible — rolling back clears exactly the rows it stamped.

Confirmed `0052` is the single leaf of the migration graph, so no merge
migration is needed.

---

# Test plan

I could not run this code — there's no database or `.env` in my
environment. Please run these before deploying.

### 1. Migration (on a COPY of production data first)

```bash
python manage.py migrate myapp 0052 --plan     # review
python manage.py migrate                       # apply
```

Then confirm the backfill only touched what it should:

```bash
python manage.py shell
```
```python
from myapp.Models.Transaction_models import IncomingPayment as P
# Provisional rows must have a rate + gross_pkr but NO fee-derived values.
bad = P.objects.filter(is_rate_provisional=True).exclude(
    net_pkr__isnull=True, fee_amount_foreign__isnull=True)
print("MUST BE 0:", bad.count())
print("no rejected stamped:",
      P.objects.filter(is_rate_provisional=True, status="rejected").count())
```

Rollback rehearsal (on the copy):
```bash
python manage.py migrate myapp 0051
```

### 2. Default rate on creation

- Submit a new payment as a customer → confirm `exchange_rate` is
  populated and `is_rate_provisional` is `true`.
- Set `default_dollar_rate = 280` in settings, submit another →
  confirm it uses `280` rather than the live rate.
- Clear the setting, delete all `ExchangeRate` rows, submit again →
  confirm the payment is still **created successfully** with no rate.

### 3. The report itself

- Open Reports → **Monday to Monday report**.
- **Critical:** confirm it shows data *without* verifying anything or
  entering any rate. That is the bug being fixed.
- Confirm rejected transactions appear **nowhere** in any total.
- Create a payment with `occurred_on` = last Friday, entered today
  (Monday) → confirm it lands in **last** week, not this week.
- Expand a week → confirm line items match the summary counts.

### 4. Regression — verify nothing else moved

This is the part I'd most want checked, since the rate field is shared:

- Open the existing **Closing report** for a past period and compare the
  profit total against a figure you recorded *before* migrating.
  **It must be unchanged.**
- Run an accountant apply on a provisional transaction → confirm
  `is_rate_provisional` flips to `false` and the fee/net figures compute
  from the *real* rate, not the placeholder.
- Check one partner's ledger balance before and after the migration —
  **must be identical**.

If the closing-report profit or a partner balance moves even slightly,
stop and roll back to `0051`; that would mean a placeholder leaked into
fee math, which the design forbids.
