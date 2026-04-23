"""
Custom S3 storage backend that never calls HeadObject/GetObject to
check for file existence.

Why this exists:
  - Our S3-compatible bucket's IAM policy grants only s3:PutObject
    (write) — not s3:GetObject or s3:HeadObject. This is deliberate;
    we serve reads via signed URLs, never through the application
    servers themselves.
  - Django's default file-storage contract tells Storage to call
    `.exists(name)` before writing, so `get_available_name()` can
    append a suffix if a filename collides. For S3Storage, `.exists()`
    translates to HeadObject — which our IAM policy rejects with 403.
    That 403 bubbles up as an `Internal Server Error` on any upload
    endpoint (company logo, KYC doc, invoice PDF cache, etc.).
  - We already set `AWS_S3_FILE_OVERWRITE = True`, which django-
    storages honours in most write paths — but its
    `get_available_name()` still falls back to the parent class which
    DOES call `.exists()`. This subclass closes that gap.

The safety of overwriting is defensible: every upload_to path is
already unique per user (profile pictures live under `avatars/<uuid>`,
company logos under `customer_companies/logos/<uuid>`, invoice PDFs
are named `<invoice_number>.pdf` and are user-scoped). A true
collision would require two simultaneous uploads with identical
filenames in the same prefix, which is effectively impossible.
"""
from storages.backends.s3 import S3Storage


class SilentS3Storage(S3Storage):
    """S3Storage variant that never reads the bucket.

    Two overrides:
      - `exists()` always returns False. Django's default flow calls
        this during `get_available_name()` to decide whether to append
        a suffix; returning False means "go ahead, write at this key."
      - `get_available_name()` short-circuits to `name` so we don't
        even attempt the existence probe. Belt-and-braces in case a
        future django-storages version changes its flow.

    We do NOT override `url()` or `open()` — those paths ALREADY work
    from the server side because they hit signed-URL generation or
    pre-signed GET endpoints that your bucket policy allows, OR they
    are only ever consumed by the browser via signed URLs (never by
    the backend itself in a way that would need bucket read rights).
    """

    def exists(self, name):
        # Always claim the file isn't there, so collision-checks skip.
        # Any real duplicate name overwrites — which is exactly what
        # `AWS_S3_FILE_OVERWRITE = True` asks for anyway.
        return False

    def get_available_name(self, name, max_length=None):
        # Parent would call `self.exists(name)` in a loop. We want to
        # skip that logic entirely and just accept `name` as-is.
        # `max_length` is honoured as a truncation so admin-defined
        # path limits still apply.
        if max_length is not None and len(name) > max_length:
            name = name[:max_length]
        return name
