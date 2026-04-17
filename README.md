# PayBitnex — Session 1 Backend Patch

This is a **patch** — unzip over your existing backend root. The 12 files
here replace their counterparts (or add new ones at the paths shown).

## What this delivers

1. **Full activity tracking** — every create/update/delete on every model
   is auto-logged to `AuditLog` via Django signals. Actor, IP, UA,
   before/after field diffs — all captured.

2. **Users — full admin CRUD**
   - `GET   /accounts/users/`
   - `POST  /accounts/users/`                   (create, returns temp password)
   - `PATCH /accounts/users/{id}/`              (edit name/role/phone/active)
   - `DELETE/accounts/users/{id}/`              (delete)
   - `POST  /accounts/users/{id}/reset-password/`
   - `POST  /accounts/users/{id}/toggle-active/`
   - `GET   /accounts/users/{id}/profile/`      (customer's KYC profile)
   - `GET   /accounts/users/{id}/score/`        (customer's computed score)

3. **Onboarding review for admin / accountant**
   - `GET /accounts/customers/onboarded/?kyc=pending|approved|rejected|all`

4. **Customer scoring**
   - `GET /accounts/score/`                     (customer's own score)
   - Auto-computed: completions, completion rate, rejection rate, volume, tenure
   - Output: 0–100 score + grade (A+ → F) + tier (vip / trusted / standard / caution / high-risk)

5. **Global activity feed**
   - `GET /core/activity/?action=…&target_model=…&target_id=…&q=…`
   - Same URL aliased at `/core/audit-log/`
   - Drives the new Admin → Activity page and per-entity timelines

6. **Selfie required on customer profile** — enforced at serializer level.
   The model field is already nullable so migrations are not required.

## Files

```
myapp/Utils/activity_signals.py       NEW   — signal-based auto logger
myapp/Utils/audit_middleware.py       REPLACE — request-context plumbing
myapp/Utils/customer_scoring.py       NEW   — score calculator
myapp/Utils/async_helpers.py          (kept, no change since last patch)
myapp/Views/Account_views.py          REPLACE — expanded CRUD + scoring
myapp/Views/Core_views.py             REPLACE — activity endpoint w/ filters
myapp/Views/Auth_views.py             (kept, no change since last patch)
myapp/Urls/Account_urls.py            REPLACE
myapp/Urls/Core_urls.py               REPLACE
myapp/serializers/User_serializers.py REPLACE
myapp/serializers/Profile_serializers.py REPLACE
myapp/serializers/Core_serializers.py REPLACE
```

## After applying

No migrations needed (nothing changed on models).

```bash
python manage.py check
python manage.py runserver
```

Test the new flows:

- **Admin → Users** → any user row opens a detail drawer with edit /
  reset-password / toggle-active / delete buttons + a live activity
  timeline scoped to that user.
- **Admin → Onboarding** → new customers' CNIC + selfie, one-click
  approve/reject with optional notes.
- **Admin → Activity** → full filterable feed of everything happening on
  the platform.
- **Customer dashboard** → now shows their auto-calculated score card at
  the top.

## Rollback

If anything's wrong, each file replaces a named predecessor — your git
history holds the originals. The patch does not drop tables, run
migrations, or touch data.
