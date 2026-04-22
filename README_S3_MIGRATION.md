# S3 Migration — Deploy Guide

This release moves file storage from Cloudinary (public URLs) to a
private S3 bucket with pre-signed URLs. New uploads go to S3
immediately. Old Cloudinary files keep working (their URLs are
frozen into invoice snapshots) but no new files land there.

## What changed

| Concern | Before | After |
|---------|--------|-------|
| Upload target | Cloudinary public CDN | Private S3 bucket |
| File URLs | Public, permanent | Signed, 60-min TTL |
| Invoice share URLs | Cloudinary `fl_attachment` flag | S3 `ResponseContentDisposition`, TTL matches `expires_at` |
| Bucket visibility | — | Block all public access = ON |

## Prerequisites (do on AWS before deploying)

1. **IAM user with a scoped policy.** Create a new IAM user (or
   attach this policy to an existing one). Replace `<YOUR-BUCKET>`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "s3:GetObject",
           "s3:PutObject",
           "s3:DeleteObject"
         ],
         "Resource": "arn:aws:s3:::<YOUR-BUCKET>/backup/documents/paybitnexdocuments/*"
       },
       {
         "Effect": "Allow",
         "Action": "s3:ListBucket",
         "Resource": "arn:aws:s3:::<YOUR-BUCKET>",
         "Condition": {
           "StringLike": {
             "s3:prefix": "backup/documents/paybitnexdocuments/*"
           }
         }
       }
     ]
   }
   ```

2. **Bucket settings.** In the S3 console, on your bucket:
   - Permissions → **Block all public access** → **ON** (all four checkboxes)
   - Properties → **Bucket Versioning** → Enable (recoverable deletes)
   - Properties → **Default encryption** → SSE-S3 (at minimum)

3. **Bucket CORS.** Permissions → CORS → paste this (edit origins):

   ```json
   [
     {
       "AllowedHeaders": ["*"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedOrigins": [
         "https://paybitnex.netlify.app",
         "http://localhost:5173"
       ],
       "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
       "MaxAgeSeconds": 3000
     }
   ]
   ```

   Without CORS the browser will block signed URLs loaded via
   `<img src>` or `<object data>`.

## Deploy steps

From inside the backend directory with your venv active:

```bash
# 1. Install the new packages.
pip install -r requirements.txt

# 2. Update your .env with the AWS variables. See .env.example
#    for the exact names. You need at least:
#      AWS_ACCESS_KEY_ID
#      AWS_SECRET_ACCESS_KEY
#      AWS_STORAGE_BUCKET_NAME
#      AWS_S3_REGION_NAME
#      AWS_S3_LOCATION=backup/documents/paybitnexdocuments

# 3. Sanity check — this will fail with a clear error if the
#    credentials are wrong or the bucket is unreachable.
python manage.py shell -c "
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
path = default_storage.save('health-check.txt', ContentFile(b'ok'))
print('uploaded:', path)
print('signed url:', default_storage.url(path))
default_storage.delete(path)
print('deleted ok')
"

# 4. Restart the Django server.
python manage.py runserver   # dev
# or: systemctl restart paybitnex   # prod
```

After step 3 you should see an `https://<bucket>.s3....amazonaws.com/...?X-Amz-Signature=...`
URL. Open it in a private-browser tab — it should serve the text
file. Wait 60 minutes and refresh — it should 403. That's the
proof signed URLs are working.

## How signed URLs now flow

- **Authenticated portal** (logged-in customer viewing their own
  invoices, KYC docs, company logos): every image/PDF URL is a
  60-minute signed URL. User opens the page → fresh URLs are
  generated. If they leave the tab open for 61 minutes and try
  to reload an image, it 403s; any navigation regenerates them.

- **Public invoice share page** (client clicking the email link):
  the signed URL for the PDF is generated with
  `TTL = max(60s, expires_at - now)`. If the customer set a
  30-day expiry, the client can save the PDF any time in those
  30 days. After `expires_at`, the share-page itself 410s, and
  any already-downloaded URL also expires at the S3 level.

## What about old Cloudinary files?

Nothing — they keep working. Every invoice created before this
release has absolute Cloudinary URLs frozen into its JSON
snapshots (`company_snapshot.logo_url`, `payment_method_snapshot.qr_code_url`,
etc.). The serializers check which backend the current `.pdf_file`
lives on and use the right download-forcing mechanism:

- New invoices (`pdf_file` on S3) → pre-signed URL with `Content-Disposition`
- Old invoices (`pdf_file` on Cloudinary) → Cloudinary `fl_attachment:` flag

You don't need to do anything for old files. They serve from
Cloudinary and are public, which is how they were before. If at
some point you want them private too, you'd write a one-time
migration command to re-upload everything — not needed for now.

## Rolling back

If something goes wrong and you need to revert to Cloudinary:

1. In `settings.py`, replace the `STORAGES` block with:
   ```python
   DEFAULT_FILE_STORAGE = "cloudinary_storage.storage.MediaCloudinaryStorage"
   ```
   (delete the new `STORAGES` dict entirely)
2. Make sure `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
   `CLOUDINARY_API_SECRET` are set in `.env`.
3. Restart Django.

Files uploaded to S3 in the meantime will become broken links
(they're stored in S3, not Cloudinary) — but that's fine if the
rollback is quick. You can flip back forward once the issue
is resolved.

## Troubleshooting

**Error: `Access Denied` on upload**
Your IAM policy is missing `PutObject` on the correct ARN.
Double-check the policy resource path includes
`/backup/documents/paybitnexdocuments/*` (note the trailing `*`).

**Error: `SignatureDoesNotMatch`**
Clock skew on your server. Run `sudo ntpdate pool.ntp.org` or
whatever your distro uses.

**Images load as 403 in the browser but work when pasted into a
fresh tab:**
CORS on the bucket is missing or misconfigured. Re-check the
CORS JSON in step 3 of prerequisites.

**Django crashes with `ImportError: No module named 'storages'`:**
You didn't run `pip install -r requirements.txt` after updating
the file. Do that and restart.

**An old invoice's PDF download is broken:**
Either the Cloudinary file was deleted, or the stored URL in
`pdf_file` no longer resolves. The download handler in
`Invoicing_serializers.py` falls back to the Cloudinary URL for
old files, but if the file itself is gone there's nothing to
recover. The regenerate-PDF button on the invoice detail page
will build a fresh PDF on S3.

## Security notes

- The `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are read
  from the environment only — **never** hardcoded.
- Signed URLs are generated with `s3v4` signatures, which include
  the request time in the signature. Replay attacks beyond the
  TTL are not possible.
- The `AWS_DEFAULT_ACL = None` setting means uploads don't attach
  any public ACL. Even if someone guessed the exact object key,
  a direct request without a signature returns 403.
- `AWS_QUERYSTRING_AUTH = True` is the critical setting —
  disabling it would make every `.url()` call return a public
  URL with no signature. Never change it.
