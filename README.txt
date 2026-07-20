PaidiX Hotfix v2 — Onboarding 404 + Activity 500
====================================================

IMPORTANT: If you applied the previous hotfix and now get a migration
error about "0003_auditlog_target_label_metadata", DELETE that file
from your myapp/migrations/ folder first. This new version does NOT
use a migration at all.

What this fixes
---------------

1) Onboarding page (HTML error on right side) — frontend calls
   /api/v1/accounts/customers/onboarded/?kyc=pending but backend
   route was /api/v1/accounts/onboarding/?kyc_status=...

   Fix: added an alias route + view accepts both param names and
   treats `all` as no filter.

2) Activity page 500 (target_label not valid for model AuditLog) —
   the serializer and signals referenced columns the installed DB
   didn't have.

   Fix: both the serializer and the activity signals now introspect
   the actual database table at startup and only reference columns
   that really exist. No migration needed.

How to apply
------------

1. IF YOU APPLIED THE PREVIOUS HOTFIX, first delete:
     myapp/migrations/0003_auditlog_target_label_metadata.py

2. Extract this zip over the root of your backend folder
   (preserves myapp/... paths).

3. Restart the Django server. No migrate needed.

Files
-----
  myapp/Urls/Account_urls.py            (replaced)
  myapp/Views/Account_views.py          (replaced)
  myapp/serializers/Core_serializers.py (replaced)
  myapp/Utils/activity_signals.py       (replaced)
