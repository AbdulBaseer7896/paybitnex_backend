# Update 2 — Vendor customers & vendor portal

## What was built

An existing customer can be linked to a vendor and given a login that
shows them **the card payments we made to them** — with their own
dashboard, transaction list and CSV export.

## Three decisions I made

You didn't specify these, so I chose conservatively. Each is a small,
localised change if you want it different.

### 1. Vendors keep `role='customer'` — no fourth role

A vendor is "also a customer but a vendor customer", so access is
*additive*: the `Vendor.portal_user` link plus a `portal_enabled` switch.

Adding a fourth `UserRole` would have meant auditing every
`role == 'customer'` check across permissions, serializers, route guards
and the payment-creation gate. Any one missed becomes a silent access
change on a live account. It would also stop a vendor from continuing to
trade as a normal customer.

### 2. Scope is the NARROW reading

A vendor sees exactly:

```
dest_vendor == their own vendor   AND   source_type == 'credit_card'
```

That is, the card payments *we made to them*. **Not** company-wide card
spend.

The broad reading of "all the transactions made with our cards" would
expose your entire card ledger and every other vendor's payments to an
external party. That's not something to enable by inference.

**To switch to the broad reading**, change `_vendor_scope()` in
`Views/Vendor_portal_views.py` — it is the only queryset in the file.

### 3. Both provisioning paths

Admin can either link an existing customer or create a fresh account
inline. New accounts get a one-time temporary password displayed for
handover.

## Security design

**One queryset.** Every vendor-facing read goes through `_vendor_scope()`.
Verified: `InternalTransaction.objects` appears exactly once in the
portal views file.

**No vendor id from the client.** The vendor is always resolved from
`request.user`. Verified: no endpoint reads a vendor identifier from
query params or the body, so a vendor cannot widen their own scope.

**OneToOne link.** `portal_user` is a `OneToOneField`, so the database
guarantees one login maps to at most one vendor. There is no path by
which a vendor user resolves to two vendors.

**Allow-list serializer.** `VendorTransactionSerializer` lists its fields
explicitly and must never become `__all__`. Withheld:

| Withheld | Why |
|---|---|
| `card_dollar_rate`, `card_profit_pkr` | our PKR conversion + rupee pool |
| `fee_dist_type`, `fee_dist_partner` | internal partner arrangements |
| `pk_*`, `fee_expense` | internal bookkeeping |
| `created_by` | which staff member entered it |
| card `last4` | vendors don't need our card numbers |

**Staff do NOT auto-pass `IsVendorPortalUser`.** This is deliberate and
unusual. If admins passed, these endpoints would be exercised almost
entirely by privileged users in testing and a scoping bug could hide.
**Test as a vendor.**

**Revocation is immediate.** `has_portal_access` requires the switch on
AND a linked AND active user. Every endpoint re-checks server-side, so a
stale open tab loses access on its next request.

**Grant/revoke never happen via PATCH.** They go through dedicated,
audited endpoints. `VendorSerializer` exposes portal status read-only.

## API

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /vendor/me/` | vendor | profile |
| `GET /vendor/dashboard/` | vendor | totals, by-card, recent |
| `GET /vendor/transactions/` | vendor | paginated, filterable |
| `GET /vendor/transactions.csv` | vendor | export |
| `GET .../vendors/portal-candidates/` | admin | eligible customers |
| `POST .../vendors/<id>/grant-portal/` | admin | link or create + link |
| `POST .../vendors/<id>/revoke-portal/` | admin | disable or unlink |

## Files

**Backend — new**
```
myapp/Views/Vendor_portal_views.py       vendor-facing (scoped)
myapp/Views/Vendor_admin_views.py        grant / revoke / candidates
myapp/Urls/Vendor_urls.py
myapp/migrations/0053_vendor_portal_access.py
```

**Backend — modified**
```
myapp/Models/InternalTx_models.py        + 4 portal columns on Vendor
myapp/Views/Auth_views.py                + is_vendor in /me
myapp/serializers/User_serializers.py    + is_vendor in login payload
myapp/serializers/InternalTx_serializers.py  + portal status (read-only)
myapp/Urls/InternalTx_urls.py            + 3 admin routes
paybitnex/urls.py                        + /vendor/ mount
```

**Frontend — new**
```
src/pages/vendor/VendorDashboard.jsx
src/pages/vendor/VendorTransactions.jsx
src/pages/admin/VendorPortalModal.jsx
```

**Frontend — modified**
```
src/routes/guards.jsx        + homeFor(), VendorRoute, onboarding exemption
src/app/App.jsx              + /vendor routes, RootRedirect fix
src/layouts/PortalLayout.jsx + vendor nav (desktop + mobile) via navKey
src/pages/auth/LoginPage.jsx + homeFor() so vendors land on /vendor
src/pages/admin/InternalRefTabs.jsx  + portal column & Manage button
```

## Migration 0053

Purely additive: four nullable/defaulted columns on `internal_vendors`.
Existing vendors get `portal_user=NULL`, `portal_enabled=False` — i.e. no
portal access, exactly as before. No backfill.

Both FKs are `SET_NULL`: deleting a user must never cascade into deleting
a vendor, which carries financial history.

**Confirmed 0053 is the single leaf of the migration graph.**

---

# ⚠ Deploy order matters

`/auth/me/` and the login serializer both read the new `portal_*`
columns. Both are wrapped in `.only()` + a broad `except` (matching the
existing KYC pattern), so a missing migration degrades to "not a vendor"
rather than crashing.

**But do not rely on that.** Apply `0053` *before* deploying this code.
If those guards were ever removed, an unapplied migration would raise
`UndefinedColumn` on login — breaking sign-in for **every** user, not
just vendors.

```bash
python manage.py migrate        # 0052 then 0053
# then deploy the app code
```

---

# Test plan

I could not run any of this — no database, no server, no requests. Please
run these before deploying.

### 1. Migration

```bash
python manage.py migrate myapp 0053 --plan
python manage.py migrate
```

Confirm existing vendors are untouched:

```python
from myapp.Models.InternalTx_models import Vendor
print("must be 0:", Vendor.objects.filter(portal_enabled=True).count())
```

Rollback rehearsal on a copy: `python manage.py migrate myapp 0052`

### 2. Granting access

- Admin → Account Settings → Vendors. Confirm the new **Portal access**
  column and **Manage** button.
- Link an existing customer → confirm the badge turns green.
- Create a new account inline → confirm the temporary password appears
  once, and that you can log in with it.
- Try linking a customer already linked elsewhere → must be **rejected**.
- Try linking a vendor that already has an account → must return **409**
  and require explicit "Move access".

### 3. THE CRITICAL TEST — cross-vendor isolation

This is the one that matters. **Do not skip it.**

Set up **two** vendors (A and B) with **two** accounts, each with card
transactions recorded against them.

- Log in as vendor A. Confirm you see **only** A's transactions.
- Confirm B's transactions appear **nowhere** — not on the dashboard,
  not in the list, not in the CSV, not in the totals.
- Repeat as vendor B.
- Record a **non-card** transaction to vendor A (e.g. USA bank → vendor).
  Confirm it does **not** appear — the portal is card-only.

### 4. Revocation

- Revoke A's access while A is still logged in with a tab open.
- Confirm their next click returns 403 — not stale data.
- Confirm "Disable" keeps the link (one click to restore) and
  "Revoke & unlink" clears it.
- Confirm the customer account itself is still **active** and can still
  log into `/app` as a normal customer.

### 5. Routing

- Vendor logs in → lands on `/vendor`, **not** `/app`.
- Vendor visits `/` → redirected to `/vendor`.
- Vendor visits `/app` directly → bounced to `/vendor`.
- Non-vendor customer visits `/vendor` → bounced to `/app`.
- Admin and accountant logins are **unaffected**.
- Check the sidebar AND the mobile bottom bar show the vendor nav
  (two items), not the customer nav.

### 6. Regression — the risky bits

The login and `/me` payloads were both modified, so:

- **Log in as each role**: admin, accountant, plain customer, vendor.
  All four must work. A break here breaks sign-in for everyone.
- Confirm a plain customer with incomplete KYC is **still** forced
  through `/onboarding` (the vendor exemption must not have leaked).
- Confirm a vendor is **not** trapped in onboarding.

If login fails for any role, the cause is almost certainly migration
`0053` not being applied — apply it and retry.

---

## Verification performed

Verified: all Python compiles; all JSX parses via Babel; no unused
imports; migration `0053` is the single graph leaf; no duplicate URL
names; all 15 icon names confirmed present in `react-icons@5.5.0`;
`InternalTransaction.objects` appears exactly once in the portal views;
no endpoint accepts a vendor id from the client. A simulation of the
scoping rules passed assertions for cross-vendor isolation, non-card
exclusion, disabled-switch, deactivated-user, and the serializer
allow-list containing none of the nine withheld fields.

Two real bugs were caught during verification and fixed: a missing
`homeFor` import in `LoginPage.jsx` (would have thrown `ReferenceError`
on every login) and the mobile bottom bar keying off `role` instead of
`navKey` (vendors would have seen the customer tab bar).

**Not verified: nothing was executed.** No migration was run, no server
started, no request made. Treat section 3 as mandatory.

---

# Round 2 fixes (from your screenshots)

### 1. "Monday to Monday report" button on both dashboards
Added to the Admin dashboard (`AdminCore.jsx`) and Accountant dashboard
(`Accountant.jsx`) headers. Each deep-links to
`…/closing-reports?tab=weekly`.

To make that work, `ReportsPage.jsx` now reads its active tab from the
`?tab=` query param and keeps it in sync (via `replace`, so tab clicks
don't stack history entries). Side benefit: the report tab now survives
a page refresh and is shareable as a link.

### 2. Dark mode
**Cause:** my new files used hardcoded `bg-white` and `-50` accent tints.
Your app themes via CSS variables (`bg-paper`, `ink-*`) that swap
automatically — `bg-white` stays white in dark mode, so the dark `ink`
text became unreadable.

**Fix:** every `bg-white` → `bg-paper`, and every accent tint/ring/text
got a `dark:` variant using low-opacity 400-level colours, mirroring how
`globals.css` already handles `.badge-ok` and `.notice-info`.

Verified: 0 remaining `bg-white`, 0 tints without a dark variant across
all five new/edited files.

### 3. Vendor password change (was "looking very bad")
**Cause:** vendors had no reachable settings page *at all*. The account
menu's `settingsPath` fell through to `/app/account-settings` — a route
`VendorRoute` bounces them out of. So the link was a dead end, and a
vendor issued a temporary password had no way to change it.

**Fix:** new `VendorAccountSettings.jsx` at `/vendor/account-settings`
with profile summary, a proper password form (show/hide toggles, live
match + length validation matching the backend's `validate_password`),
and a theme picker. Added to the vendor sidebar, and `settingsPath` now
checks `isVendor` **before** the customer fallback.

### 4. Vendor dashboard filters
Quick presets (All time / Last 30 days / Last 90 days / This year) plus
custom from/to date inputs. The backend already accepted
`date_from`/`date_to` on `/vendor/dashboard/` — this only wires the UI.

### 5. "View all" error — FIXED
**Cause:** it was a raw `<a href="/vendor/transactions">`, which triggers
a **full page reload**. That re-bootstraps the SPA and re-issues
`/auth/me/` — the request failing with `ERR_SSL_PROTOCOL_ERROR` in your
console.

**Fix:** replaced with React Router's `<Link>`, so navigation stays
client-side. No reload, no error.

> **Note on the SSL errors:** those come from your browser trying HTTPS
> against the HTTP dev server (`net::ERR_SSL_PROTOCOL_ERROR` +
> `You're accessing the development server over HTTPS`). Your `api.js`
> already defends against this by forcing same-origin relative paths on
> loopback hosts. The `<Link>` fix removes the reload that was surfacing
> it. If it persists elsewhere, it's browser HSTS cache for
> `127.0.0.1:8000` — clear it at `chrome://net-internals/#hsts`, and
> prefer `localhost:5173` so the Vite proxy handles the API.

## Retest after this round

- Dark mode: toggle it on the Monday-to-Monday report, the vendor
  dashboard, transactions, settings, and the vendor-portal modal.
- Click "View all" on the vendor dashboard — must navigate with **no**
  reload and no console error.
- Vendor → Settings → change password → sign out → sign in with the new
  one.
- Admin and Accountant dashboards: the new button must open the report
  **on the Monday-to-Monday tab**, not Overview.
- Vendor dashboard filters: confirm the totals actually change.

---

# Round 3 fixes

### 1. Card Payments filter layout — rebuilt
The three bare inputs floated at odd widths with no labels. Replaced with
a single bordered panel: quick-range presets on top, then a labelled
4-column grid (Search / Card / From / To) that collapses to 2 columns on
tablet and 1 on mobile. Added a "Clear N filters" control that only
appears when filters are active.

### 2. Card selection filter
New `GET /vendor/cards/` returns only the cards that have **actually paid
this vendor**, with a payment count per card.

Security note: that list is derived from `_vendor_scope()`, not from the
company's `CreditCard` table — so a vendor cannot enumerate your payment
instruments, only the ones already visible on their own statements. Card
numbers are omitted (label + brand only).

The `card` filter narrows an already-scoped queryset, so passing a
foreign card id yields **zero rows** rather than another vendor's data.
Verified by simulation.

**Also fixed while here:** `/vendor/transactions.csv` was ignoring all
filters. A vendor could filter to one card, hit CSV, and get every row
back — the export silently disagreed with the screen. It now applies the
same filters as the list view.

### 3. Dashboard charts
Two charts added, both theme-aware (they read `useTheme()` and switch
axis/grid/tooltip palettes, unlike the existing admin charts which
hardcode light-mode colours):

- **Payments over time** — monthly area chart, last 12 months.
- **Totals by card** — horizontal bar, top 6 cards.

Note: the API returns `monthly` newest-first; a time-series axis has to
read oldest-first, so the frontend re-sorts ascending before plotting.

### 4. Settings removed from the sidebar
Removed from both the desktop sidebar and the mobile bottom bar.

**Kept reachable** via the account menu (bottom-left avatar → Settings)
and the `/vendor/account-settings` route. Removing it entirely would
strand any vendor issued a temporary password with no way to change it.

## Retest

- Card Payments: confirm the filter panel aligns at desktop, tablet and
  phone widths.
- Card dropdown: confirm it lists **only** cards that paid this vendor,
  each with a count.
- Filter by card, then hit CSV — the download must match what's on
  screen.
- Dashboard: confirm both charts render, and toggle dark mode to check
  axis/tooltip colours.
- Confirm Settings is gone from the sidebar but still reachable from the
  avatar menu, and that the password form still works.

---

# Round 4 fixes

### 1. Date presets reworked (dashboard + card payments)
Now: **This month** (default) · This week · Last week · Last 30 days ·
This year · All time. "Last 90 days" removed.

This/Last week honour the company's configured closing week, fetched from
`/core/bitnex-week/`. Hardcoding Monday would have made the vendor's idea
of a week disagree with your closing reports — your week currently starts
**Tuesday**. Verified against both configs, including that Last week never
overlaps This week across every start-day/weekday combination.

### 2. Refresh logged the vendor out — FIXED (two causes)

**Cause A — the request went to `https://127.0.0.1:8000`.**
`VITE_API_BASE_URL` is set to an absolute loopback URL in your
environment. `resolveApiBase()` only ignored it on loopback *pages*, so
any other host kept the absolute URL. `127.0.0.1` then carries its own
HSTS policy, the browser force-upgrades to `https://` below the JS layer,
and the plain-HTTP dev server kills it — `ERR_SSL_PROTOCOL_ERROR`,
surfaced as "Unable to reach the server."

Added a hard guard: a **loopback base is never used from a non-loopback
page**, and an `http://` base is never used from an `https://` page
(mixed content). Both fall back to the same-origin relative path, which
the Vite proxy already serves. Verified across six scenarios.

**Cause B — a hard refresh had no user to preserve.**
`fetchMe.rejected` correctly kept the session on transport errors, but on
a refresh the store starts empty, so "keep the existing user" kept `null`
and `ProtectedRoute` bounced to `/login`.

Added a last-known-user cache in localStorage, rehydrated only when the
server is unreachable **and** an access token is still present. It is not
an authorisation mechanism — every request still carries the JWT, the
server still decides, and a real 401/403 clears the cache immediately.

### 3. Vendors are not customers — feature access
The Feature access modal no longer offers Invoicing/Dispatch for a vendor.
It now explains that vendors and customers are two different kinds of
user, and points to Account Settings → Vendors.

`UserSerializer` gained `is_vendor` / `vendor_name` (with
`select_related("vendor_profile")` on the viewset so listing users stays
one query, not N+1).

> If you already granted Invoicing/Dispatch to the vendor account, revoke
> them: those grants still exist in the database. This change stops new
> ones being made, but doesn't retroactively clear old rows.

### 4. Users page — vendor filter
Dropdown now offers: All users · **Customers** (excludes vendors) ·
**Vendors** · Customers + vendors · Accountants · Admins. Vendor rows
carry a `VENDOR` badge showing the linked vendor on hover.

Backend gained a `user_type` filter, since vendors and customers share
`role='customer'` and can't be separated by role alone. Verified the
partition is disjoint and exhaustive.

## Retest
- Refresh the vendor portal repeatedly — must stay signed in.
- Stop Django, refresh: you should stay logged in with an error toast,
  not get kicked to /login.
- Users page: filter by Vendors, confirm only vendor accounts show.
- Open Feature access on the vendor — confirm the explanation, no toggles.
- Both vendor pages should open on **This month**.

---

# Round 5 — console 404s

Good news first: the log shows requests now going to
`localhost:5173/api/v1/...` — same-origin via the Vite proxy. The
`ERR_SSL_PROTOCOL_ERROR` / refresh-logout problem is resolved.

The remaining 404s were **pre-existing bugs**, unrelated to the vendor
work. They were silent because the frontend swallowed them in empty
`catch` blocks — the admin Users detail drawer has simply never shown a
customer's KYC profile or score.

### Two real bugs

**1. `users/<id>/profile/` and `users/<id>/score/` had no routes.**
The frontend called both; neither existed. Registered them, pointing at
the existing views.

**2. `score/<int:user_id>/` could never match.**
`User.pk` is a UUID, so the `<int:...>` converter rejected every request.
That staff score lookup has been dead since it was written. Corrected to
`<uuid:user_id>`.

`CustomerProfileView.get()` also only ever read `request.user`, so it had
no way to serve a staff lookup. It now accepts an optional `user_id`.

### Security

`user_id` is honoured **only** for admin/accountant. A customer passing
another user's id still gets their own row. Vendors count as customers
here, so they can't read anyone else's profile either. Verified by
simulation across five caller/target combinations.

`GET` only. `POST`/`PATCH` now explicitly return **405** when a `user_id`
is present, so the staff read-route can never be turned into a
cross-user write. Previously they'd have raised a `TypeError` (500) —
not exploitable, but sloppy.

Malformed UUIDs return 404 rather than 500 (`ValidationError` is now
caught — it was referenced but not imported, which would itself have been
a `NameError`).

### Also

The Users drawer no longer requests profile/score for **vendor**
accounts. Vendors share `role='customer'` but never complete KYC, so
those calls always 404'd with nothing to show.

### Not a bug

The two React Router "Future Flag" warnings are v7 deprecation notices,
harmless on v6. Silence them by passing `future={{ v7_startTransition:
true, v7_relativeSplatPath: true }}` to your router when you're ready to
migrate. `chext_driver.js` is a browser extension, not your app.

---

# Round 5 — Reference column replaced with Document

**Card Payments table** now reads: Date · Card · Method · Description ·
**Document** · Amount. The Reference column is gone (it was empty on
every row anyway — internal card transactions rarely carry a bank ref).

The Document column shows a "View" link opening the attached receipt in a
new tab, or "—" when there's no attachment. The dashboard's Recent
payments list gained the same link.

**CSV export** swapped its Reference column for Document (the URL), so
the download matches the table.

**Search placeholder** changed from "Reference or description" to
"Search description" — promising to search a column the vendor can no
longer see would be misleading. The backend still matches reference
behind the scenes, which is harmless and useful if they know a ref from
correspondence.

## Notes

- `document_url` is served through the normal storage backend, so on S3
  it's a **signed, expiring URL** — consistent with how the rest of the
  app serves attachments.
- Both the serializer and the CSV writer access `.url` defensively. A
  legacy row pointing at a missing file, or a storage misconfiguration
  (see `S3_REGION_FIX.md`), yields "—" rather than 500-ing the whole
  list.
- Scope is unchanged: documents are serialized from rows already filtered
  by `_vendor_scope()`, so a vendor only ever sees attachments on their
  own transactions. The serializer remains an explicit allow-list.

**Related:** while the S3 region typo is unfixed, uploads land on local
disk, so documents attached during that window won't be in your bucket.
