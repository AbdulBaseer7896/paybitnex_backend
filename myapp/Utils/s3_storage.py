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

Uniqueness note (bug fix):
  Overwrite-on-collision turned out NOT to be safe: proof screenshots
  from bulk payment entry often share a filename ("screenshot.png"),
  so several transactions' images collapsed onto one S3 key and every
  record showed the last image uploaded. `get_available_name()` below
  now ALWAYS uniquifies the stored name with a random suffix, so each
  transaction keeps exactly the image the user attached to that row.
"""
from storages.backends.s3 import S3Storage
import os
import uuid


class SilentS3Storage(S3Storage):
    """S3Storage variant that never reads the bucket.

    Two overrides:
      - `exists()` always returns False. Django's default flow calls
        this during `get_available_name()` to decide whether to append
        a suffix; returning False means "go ahead, write at this key."
      - `get_available_name()` can't probe the bucket (no read rights),
        so instead of trusting the raw name it ALWAYS makes the key
        unique by inserting a short random hex suffix before the
        extension.

    WHY the forced-unique suffix (bug fix):
        With plain overwrite-on-collision, a customer bulk-submitting
        several payments whose screenshots share a filename (phones and
        Windows love "screenshot.png") would have every upload land at
        the SAME key — each write overwriting the previous — so every
        payment record ended up pointing at the LAST image uploaded.
        The proof images looked "merged" across transactions. Suffixing
        every stored name (e.g. `proofs/txn/screenshot_a1b2c3d4.png`)
        guarantees each transaction keeps exactly the image the user
        attached to that row, with zero bucket reads required.

    We do NOT override `url()` or `open()` — those paths ALREADY work
    from the server side because they hit signed-URL generation or
    pre-signed GET endpoints that your bucket policy allows, OR they
    are only ever consumed by the browser via signed URLs (never by
    the backend itself in a way that would need bucket read rights).
    """

    def exists(self, name):
        # Always claim the file isn't there, so collision-checks skip.
        return False

    def get_available_name(self, name, max_length=None):
        # We can't check the bucket for collisions (write-only IAM), so
        # never trust the incoming name: append a random 8-hex suffix
        # before the extension. Django's FileField stores whatever name
        # we return here, so no other code needs to know.
        dir_name, file_name = os.path.split(name)
        root, ext = os.path.splitext(file_name)
        suffix = uuid.uuid4().hex[:8]

        # Honour max_length by trimming the ROOT, never the suffix or
        # extension — the suffix is what guarantees uniqueness.
        candidate = os.path.join(dir_name, f"{root}_{suffix}{ext}")
        if max_length is not None and len(candidate) > max_length:
            overflow = len(candidate) - max_length
            root = root[:-overflow] if overflow < len(root) else ""
            candidate = os.path.join(dir_name, f"{root}_{suffix}{ext}")
            if max_length is not None and len(candidate) > max_length:
                # Pathological max_length — fall back to suffix+ext only.
                candidate = os.path.join(dir_name, f"{suffix}{ext}")[:max_length]
        return candidate
