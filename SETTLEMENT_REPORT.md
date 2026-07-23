# Settlement report — rewritten

## What I got wrong

I built a week-by-week listing. You wanted the answer to a money
question: **"how much have we not yet transferred to customers?"**

Your screenshot showed the real problem — 10 transactions, $8,564
received, and every PKR figure reading **Rs 0.00**, because the system
only computes PKR once an accountant applies a real dollar rate.

## What it does now

Click **Monday to Monday report** and every unprocessed transaction is
projected at the default dollar rate, so all downstream figures populate:

| Metric | Before | After (rate 280, fee 5%) |
|---|---|---|
| Total transactions | 10 | 10 |
| Total submitted | $8,564.00 | $8,564.00 |
| Total received (PKR) | Rs 0.00 | **Rs 2,397,920.00** |
| Owed to customers (net) | Rs 0.00 | **Rs 2,278,024.00** |
| Fee revenue (PKR) | Rs 0.00 | **Rs 119,896.00** |
| **Still to transfer** | — | **Rs 2,278,024.00** |

## Projected vs actual — never blended

Each transaction is classified:

- **ACTUAL** — accountant applied a real rate. Figures come straight from
  the stored columns, never recomputed.
- **PROJECTED** — no real rate yet. Computed on the fly from the default
  rate and that customer's effective fee.

Every total is returned three ways — `actual_*`, `projected_*`, and
combined — so the UI always shows what is real versus estimated.
Projected rows carry an `EST` marker.

**Projections are never written to the database.** The endpoint is
read-only. The accountant applying a real rate stays the single source of
truth, so nothing here can corrupt a partner ledger or a payout.

### Rate-spread profit is deliberately actual-only

Spread = (real market rate − customer rate). The real rate only exists
once an accountant records it, so projecting a spread would fabricate
profit out of nothing. It stays Rs 0.00 until the real rate is entered.

### Rate + fee resolution mirrors the live pipeline

```
rate = SystemSetting["default_dollar_rate"] → ExchangeRate table
fee  = CustomerFeeConfig[customer] → SystemSetting["default_fee_percentage"] → 5.00
```

The arithmetic is a line-for-line mirror of `calculate_amounts()`,
including identical `quantize(0.01)` rounding at each step — so a
projected figure equals exactly what the row will show once the
accountant applies that same rate.

## By customer

The report groups by customer: transaction count, per-status breakdown,
USD total, PKR owed, and how much is still pending. Click a customer to
expand every transaction with a subtotal row. A grand total sits at the
bottom.

## Sheet view

The **Sheet view** button opens a spreadsheet-style grid: row numbers,
cell borders, sticky header and footer, customer group headers, per
customer subtotals, and a grand total. There's a CSV export.

**It is read-only by design.** These figures are derived and partly
projected, so an edit would have nowhere to go — changing a rate belongs
in the accountant's apply-rate flow, which is audited.

## Note on the tab name

The tab is now labelled **Settlement** rather than "Monday to Monday",
since it answers a settlement question rather than showing a weekly
calendar. The dashboard button still says "Monday to Monday report" as
you asked. Say the word if you'd rather both match.

## Verification

Simulated your exact screenshot (10 tx, $8,564) and confirmed:
- `gross == amount × rate`
- `fee == gross − net`
- projected spread is always 0
- actual and projected never blend
- sheet view rows all align to 10 columns

**Not verified:** nothing was run against a database.

## Test

1. Set `default_dollar_rate` in System Settings (e.g. `280`) — without
   it, projections can't compute and the banner says so.
2. Open Reports → Settlement. The PKR figures should be populated.
3. Check "Still to transfer" against what you expect to owe.
4. Expand a customer; the subtotal must match their row.
5. Open Sheet view; the grand total must match the headline.
6. Apply a real rate to one transaction, refresh — its `EST` marker
   should disappear and it should move from projected to actual.

---

# Round 2 — dark-mode fix + in-place dashboard projection

## 1. Sheet colours — root cause found

The washed-out rows were my mistake. Your `ink` scale **already inverts**
in dark mode (`globals.css` → `html.dark`): `ink-50` stays a deep surface
and `ink-900` becomes near-white, so `text-ink-900` keeps its meaning in
both themes.

I had added `dark:` overrides on top of that, which double-inverted:

```
bg-ink-100 dark:bg-ink-800   →  dark:bg-ink-800 = rgb(223,233,230) = NEAR WHITE
bg-ink-200 dark:bg-ink-700   →  dark:bg-ink-700 = rgb(211,224,221) = NEAR WHITE
```

Removed all 39 of those overrides across six files. `bg-ink-100` alone is
already `rgb(20,66,69)` in dark mode — correct.

Accent tints (amber/emerald/brand) **keep** their `dark:` variants, since
those are fixed Tailwind palettes that don't invert.

## 2. Dashboard button now updates in place

The **Monday to Monday report** button on the Overview page no longer
navigates to Closing Reports. It toggles projection and recomputes the
stats already on that page:

- Total received (PKR)
- PKR transferred to customers
- Company profit
- Fee-charged profit (PKR)
- Rate-spread profit (PKR)
- PKR reconciliation

Nothing new was added to the page. The button turns solid while active
and reads "Showing projected totals"; a banner states the rate used, how
many transactions were projected, and how much PKR that adds.

Rate-spread profit stays **actual-only** even when projecting — it needs
the real market rate, which only exists once an accountant records it.

### Math parity with the backend — verified

The frontend projection mirrors `calculate_amounts()` step for step, with
the same round-to-2dp at each stage. Checked against your screenshot:

| Amount | Computed | Screenshot |
|---|---|---|
| 111 | 31,003.74 | 31,003.74 |
| 4,444 | 1,241,266.97 | 1,241,266.97 |
| 666 | 186,022.46 | 186,022.46 |

(Your true rate is 279.313 — the UI's `279.31` is display rounding only.
The projection reads the unrounded setting, so it agrees to the paisa.)

### Note on the accountant dashboard

Its button still opens the full report. That page shows a work queue, not
the PKR stat cards, so there is nothing there to update in place.

## Test
1. Toggle dark mode on the Settlement sheet — rows should be dark with
   readable text, not white.
2. Overview → click **Monday to Monday report**. It must stay on the
   page, turn solid, and the PKR cards should populate.
3. Click again to return to actual-only figures.
4. Rate-spread profit should stay Rs 0.00 while projecting unless real
   rates exist.

---

# Round 3

## 1. Per-customer fees — already working; now made visible

The per-customer fee config **was** being applied. Abdul Basir showed
15% because he has **no override** — only zain (15%), hassan (12%) and
adan (9%) are configured, so he correctly falls back to the 15% system
default. Same number, different reason.

Verified against your actual config:

| Customer | Fee | Source | Net PKR on $4,444 |
|---|---|---|---|
| abdulbasirqazi7896@ | 15% | system default | 1,055,076.93 |
| zain@ | 15% | override | 1,055,076.93 |
| hassan@ | 12% | override | 1,092,314.94 |
| adan@ | 9% | override | 1,129,552.94 |

Different fees produce different net figures, so the lookup is live.

**Change made:** each line item now returns `fee_source`
(`customer_override` / `system_default` / `transaction`), so a uniform
fee across customers reads as "no override configured" rather than
looking like the override was ignored.

The dashboard banner no longer claims a single fee for everything. It now
says each customer's own fee applies, and states how many overrides
exist.

> To give Abdul Basir a different rate, add an override under
> Settings → Fee config. The report picks it up immediately.

## 2. Date range on the Overview projection

The banner now reads "Showing projected totals for Jul 1, 2026 – Jul 23,
2026", derived from the active range filter (or "(all time)" when
unbounded).

## 3. Dark-mode bug — the invisible "8 txn" badge

Two leftover `dark:text-ink-200` overrides — one on the txn-count chip,
one on the sheet header. In dark mode `ink-200` is `rgb(18,60,63)`, a
*dark* colour sitting on the dark `ink-100` chip, so the text vanished.

Removed both. The ink scale already inverts, so plain `text-ink-700` is
correct in both themes. Zero `dark:text-ink-*` overrides now remain in
these files.

## 4. Sheet view size

Was `max-w-[1400px] h-full` — effectively a second full page. Now
`max-w-4xl max-h-[75vh]`, a compact modal that scrolls internally with
the header and grand-total row pinned.

---

# Round 4

## 1. "Monday to Monday" now means last week-start → today

It was showing Jul 1 → Jul 23 (the whole month) because it reused the
page's range filter. The projection now has its **own** window,
defaulting to the most recent week-start through today.

It honours the configured closing-week start day, so with your
Tuesday-start week:

```
Today Thu 23 Jul 2026  ->  Tue 21 Jul  ->  Thu 23 Jul
```

Verified across 42 day/start-day combinations: the window is always 0–6
days long and always begins on the configured day.

## 2. Editable from/to on the Overview banner

The banner now carries two date inputs plus a "Reset to this week" link.
Change them and the projected figures recompute for exactly that range —
only unprocessed transactions inside the window are valued.

Rows are matched on `occurred_on` (falling back to entry date), the same
basis the backend report uses, so a Friday batch keyed in on Monday still
lands in Friday's week.

This window is deliberately independent of the page's own range filter:
the filter controls which transactions the dashboard counts, while this
controls which unprocessed ones get valued. Mixing them is what produced
the whole-month range.

## 3. Sheet view width

`max-w-4xl` was too narrow — Reference and Status wrapped onto two lines.
Now `max-w-6xl` / `max-h-[80vh]`, with `whitespace-nowrap` on both
columns so the grid scrolls horizontally rather than wrapping.

Column alignment re-verified: header, data, subtotal and grand-total rows
all at 10 columns.

## Note
`formatDate` became unused in AdminCore and was removed from the import.
(`formatDateTime` is also unused, but it was already that way in your
original file, so I left it.)

---

# Round 5 — the projection window governs the whole page

Previously the window only decided which *unprocessed* rows got valued,
while the preset buttons (Bitnex week / This month / Last month / All
time) still decided which transactions the page counted. Two date ranges
were live at once, which is why the header could read "Jul 1 – Jul 23"
while the window said something else.

Now, when projection is ON:

- **The window is the only date range.** `filteredPayments` ignores the
  preset entirely and filters on the window, so Total transactions,
  Total submitted, Total received, PKR transferred, Company profit,
  Fee-charged profit and the reconciliation section all reflect exactly
  the dates typed in.
- **The preset buttons and filter bar are disabled** — dimmed to 40%,
  `pointer-events-none`, and `aria-hidden` so they leave the tab order.
  They're visible for context but can't be used, so the two ranges can
  never disagree.
- **Non-date filters still work** — customer, currency and status apply
  in both modes, since those aren't in conflict with the window.

Switch projection off and the preset buttons take over again, unchanged.

### Rows match on `occurred_on`

The window filters on the business date, falling back to the entry date —
the same basis the backend report uses. A Friday batch keyed in on Monday
still counts in Friday's week.

### One source of truth

The per-row window check inside the totals loop was removed. Since
`filteredPayments` is already restricted to the window, testing it again
downstream would have been a second source of truth that could drift.

### Verified
- With preset "This month" and window Jul 21–23, a Jul 5 transaction is
  excluded — the window overrides the preset.
- Widening the window to Jun 1 pulls June rows in, proving the window is
  authoritative rather than an intersection.
- Wrapper `<div>` opens at line 554 and closes at 593, enclosing both
  control bars; JSX parses.
