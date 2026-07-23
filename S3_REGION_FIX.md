# ⚠ ACTION REQUIRED — fix your .env

## The S3 500 error

```
botocore.exceptions.EndpointConnectionError: Could not connect to the endpoint URL:
"https://my-backups-storage-bitnext-1.s3.us-east-13.amazonaws.com/..."
socket.gaierror: [Errno 11001] getaddrinfo failed
```

**`us-east-13` is not an AWS region.** It doesn't exist, so DNS can't
resolve the hostname — hence `getaddrinfo failed`. Every file upload
(internal-transaction documents, KYC docs, receipts, profile pictures)
dies with a 500.

This is a typo in your `.env`, not a code bug. The code default is
`us-east-1`.

### Fix

In your backend `.env`:

```diff
- AWS_S3_REGION_NAME=us-east-13
+ AWS_S3_REGION_NAME=us-east-1
```

Check `AWS_REGION` too if you set that instead. Then restart Django.

Confirm the region matches where the bucket actually lives — AWS console
→ S3 → your bucket → Properties → AWS Region. If it's in Ohio, the value
is `us-east-2`.

### Code hardening added

Django now validates the region at startup. If it isn't a recognised AWS
region, you get a loud `RuntimeWarning` naming the bad value and
suggesting `us-east-1`, and storage falls back to **local files** so
uploads keep working instead of 500-ing.

Custom S3-compatible providers (MinIO, Wasabi, R2, Spaces) are never
second-guessed — set `AWS_S3_ENDPOINT_URL` and the check is skipped.

> Note: while the fallback is active, uploads land on the local disk, not
> in your bucket. Fix the region and restart to resume writing to S3.

## The /profile/ and /score/ 404s

Those fire when opening a **vendor** in the admin user detail panel.
Vendors share `role='customer'` but never complete customer KYC, so both
lookups legitimately 404.

Already guarded in the build you have (`user.role === 'customer' &&
!user.is_vendor`), so the 404s you saw came from the **previously loaded
build**. Hard-refresh the browser (Ctrl+Shift+R) and they stop.

This round also adds a "Vendor account" panel explaining why the KYC and
score sections are absent, plus a `VENDOR` badge in the detail header.
