"""
Django settings for PayBitnex.
Async-first: DRF + adrf, JWT, Cloudinary, Celery, Redis.
"""
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:5173")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "cloudinary",
    "cloudinary_storage",
    # S3 backend for NEW uploads. `storages` provides
    # storages.backends.s3.S3Storage, which we wire into the
    # STORAGES setting below. Keep cloudinary_storage listed above
    # so legacy code paths that still import it don't crash —
    # they're just never selected as the default storage anymore.
    "storages",
    "drf_spectacular",
    "django_extensions",
    "anymail",
    # local
    "myapp",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "myapp.Utils.audit_middleware.AuditMiddleware",
]

ROOT_URLCONF = "paybitnex.urls"
WSGI_APPLICATION = "paybitnex.wsgi.application"
ASGI_APPLICATION = "paybitnex.asgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

DATABASES = {
    "default": dj_database_url.parse(
        config("DATABASE_URL", default="sqlite:///db.sqlite3"),
        conn_max_age=600,
    )
}

# SQLite production tuning — only applied when we're actually using SQLite.
# Django's SQLite backend doesn't accept `init_command` in OPTIONS (that's
# MySQL-only), so we wire PRAGMAs via the connection_created signal which
# fires once per new connection.
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # 20s timeout at the Python sqlite3 driver level handles brief lock
    # contention (Celery + web workers occasionally racing).
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["timeout"] = 20

    from django.db.backends.signals import connection_created

    def _apply_sqlite_pragmas(sender, connection, **kwargs):
        if connection.vendor != "sqlite":
            return
        with connection.cursor() as cursor:
            # WAL journal mode: readers don't block writers and vice versa.
            cursor.execute("PRAGMA journal_mode=WAL;")
            # Still crash-safe in WAL mode, much faster than FULL.
            cursor.execute("PRAGMA synchronous=NORMAL;")
            # Wait up to 5s for a lock instead of erroring immediately.
            cursor.execute("PRAGMA busy_timeout=5000;")
            # Enforce referential integrity (Django usually sets this too).
            cursor.execute("PRAGMA foreign_keys=ON;")

    connection_created.connect(_apply_sqlite_pragmas)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "myapp.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "myapp.Utils.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # ── API rate limiting ──────────────────────────────────────────────
    # Prevents brute-force and DoS. Anon: 60/min, authed: 300/min.
    # Login endpoint gets its own tighter scope (see Auth_urls.py or
    # add a per-view throttle class there).
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "300/min",
        "login": "10/min",   # tighter scope for /auth/login/
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=config("JWT_ACCESS_LIFETIME_MINUTES", default=60, cast=int)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=config("JWT_REFRESH_LIFETIME_DAYS", default=7, cast=int)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "PayBitnex API",
    "DESCRIPTION": "Banking-style CRM for cross-border payments.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:5173,http://localhost:3000",
    cast=Csv(),
)
CORS_ALLOW_CREDENTIALS = True

# ────────────────────────────────────────────────────────────────────
# File storage — AWS S3 (private bucket, pre-signed URLs)
# ────────────────────────────────────────────────────────────────────
# All user-uploaded media (company logos, payment-method QR codes,
# KYC documents, invoice PDFs, avatars, etc.) lives in a PRIVATE S3
# bucket. `django-storages` serves them via time-limited pre-signed
# URLs — you never expose raw bucket URLs. The bucket itself should
# have "Block all public access = ON" in the AWS console.
#
# Cloudinary is no longer the default storage but its config stays
# defined below so legacy code that still imports cloudinary_storage
# doesn't crash. Old Cloudinary-hosted files (logos, QR codes,
# snapshot URLs on old invoices) keep working because the URLs
# frozen into their JSON snapshots are absolute Cloudinary URLs.
# ────────────────────────────────────────────────────────────────────

AWS_ACCESS_KEY_ID       = config("AWS_ACCESS_KEY_ID",      default="")
AWS_SECRET_ACCESS_KEY   = config("AWS_SECRET_ACCESS_KEY",  default="")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME",
                                 default=config("S3_BUCKET_NAME", default=""))
AWS_S3_REGION_NAME      = config("AWS_S3_REGION_NAME",
                                 default=config("AWS_REGION", default="us-east-1"))

# Everything is uploaded under this key prefix inside the bucket.
# Keep the folder hierarchy you asked for:
#   backup/documents/paybitnexdocuments/<upload_to>/<filename>
AWS_LOCATION = config(
    "AWS_S3_LOCATION",
    default="backup/documents/paybitnexdocuments",
)

# Security posture — NEVER change these.
AWS_DEFAULT_ACL          = None   # Don't attach a public ACL on upload
AWS_QUERYSTRING_AUTH     = True   # Every .url() returns a SIGNED URL
AWS_S3_SIGNATURE_VERSION = "s3v4" # Required in most regions incl. us-east-1

# Overwrite-on-collision instead of asking S3 "does this key already
# exist?" before writing. The existence check requires s3:GetObject /
# s3:ListBucket on the prefix; if IAM is locked down to PutObject
# only, the HeadObject probe returns 403 and Django raises
# SuspiciousFileOperation, breaking any upload (company logo, profile
# picture, KYC doc, etc.). Django's `get_available_name()` already
# appends a random 7-char suffix to the filename, so real collisions
# across users are effectively impossible — overwriting is safe.
AWS_S3_FILE_OVERWRITE    = True

# TTL for signed URLs Django hands out for authenticated access
# (portal pages, avatars, KYC docs, etc.). 60 minutes — plenty for
# a user browsing the app, short enough that a leaked URL in a
# chat log / screenshot expires fast.
AWS_QUERYSTRING_EXPIRE = int(config("S3_SIGNED_URL_TTL", default=3600))

# Basic cache-control; private so proxies don't cache the signed URL.
AWS_S3_OBJECT_PARAMETERS = {
    "CacheControl": "private, max-age=300",
}

# Django 4.2+ STORAGES dict. This supersedes the legacy
# DEFAULT_FILE_STORAGE setting — Django 4.2 still honours the old
# name but you shouldn't mix both.
#
# We point at a custom subclass of S3Storage (SilentS3Storage) that
# never calls HeadObject/GetObject to check for existence. Our IAM
# policy on the bucket grants write-only access — the default
# S3Storage.exists() call would fail with 403 and break every
# upload (company logo, profile pic, KYC doc, invoice PDF). See
# myapp/Utils/s3_storage.py for the full rationale.
STORAGES = {
    "default": {
        "BACKEND": "myapp.Utils.s3_storage.SilentS3Storage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Cloudinary — kept defined for backwards-compat only. Not selected
# as default storage anymore. Safe to strip once no code references
# CLOUDINARY_STORAGE.
CLOUDINARY_STORAGE = {
    "CLOUD_NAME": config("CLOUDINARY_CLOUD_NAME", default=""),
    "API_KEY":    config("CLOUDINARY_API_KEY",    default=""),
    "API_SECRET": config("CLOUDINARY_API_SECRET", default=""),
}

# Redis + Celery
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "Asia/Karachi"

from celery.schedules import crontab  # noqa: E402
CELERY_BEAT_SCHEDULE = {
    "fetch-exchange-rates-hourly": {
        "task": "myapp.Utils.rate_tasks.fetch_live_rates",
        "schedule": crontab(minute=0),
    },
    "generate-daily-report": {
        "task": "myapp.Utils.report_tasks.generate_daily_report",
        "schedule": crontab(hour=0, minute=5),
    },
    # Flag PKR-sent payments that have been awaiting customer confirmation
    # beyond the configured threshold (SystemSetting `stale_payment_days`).
    # Runs shortly after midnight so a payment transitioned "today" won't be
    # flagged before the user has had at least one full day to confirm.
    "flag-stale-payments-hourly": {
        "task": "myapp.Utils.stale_payment_tasks.flag_stale_payments",
        # Run every 30 minutes so minute-level thresholds (e.g. 60 minutes
        # for testing, or anything <24h for production) actually trigger
        # within a reasonable window of the cutoff.
        "schedule": crontab(minute="*/30"),
    },
    "cleanup-expired-otps-daily": {
        "task": "myapp.Utils.email_tasks.cleanup_expired_otps",
        "schedule": crontab(hour=2, minute=0),  # 2 AM Karachi time — low traffic
    },
}

EXCHANGE_RATE_API_KEY = config("EXCHANGE_RATE_API_KEY", default="")
EXCHANGE_RATE_PROVIDER = config("EXCHANGE_RATE_PROVIDER", default="open-erapi")

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Karachi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST         = config("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT         = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS      = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL      = config("EMAIL_USE_SSL", default=False, cast=bool)
EMAIL_HOST_USER    = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL",
    default=EMAIL_HOST_USER or "no-reply@paybitnex.com",
)
EMAIL_TIMEOUT      = config("EMAIL_TIMEOUT", default=20, cast=int)

# Anymail (Resend) — used when hosting providers block SMTP ports 25/465/587.
# Set EMAIL_BACKEND=anymail.backends.resend.EmailBackend in .env to switch.
ANYMAIL = {
    "RESEND_API_KEY": config("RESEND_API_KEY", default=""),
}

# Human-readable sender display: "PayBitnex <no-reply@paybitnex.com>"
EMAIL_FROM_NAME    = config("EMAIL_FROM_NAME", default="PayBitnex")

# Frontend origin used inside emails for links like reset-password pages.
FRONTEND_URL       = config("FRONTEND_URL", default="https://paybitnex.com")

# ── Production security headers ───────────────────────────────────────
# These should also be set at the Nginx/CDN layer for belt-and-suspenders
# hardening, but setting them here ensures they're enforced even in
# non-Nginx deployments.
SECURE_BROWSER_XSS_FILTER     = True
SECURE_CONTENT_TYPE_NOSNIFF   = True   # prevents MIME-type sniffing
X_FRAME_OPTIONS               = "DENY"  # stops clickjacking

# Only enable HSTS + cookie flags in production (DEBUG=False).
# Enabling in dev would break plain-http localhost flows.
if not DEBUG:
    SECURE_SSL_REDIRECT             = True
    SESSION_COOKIE_SECURE           = True
    CSRF_COOKIE_SECURE              = True
    SECURE_HSTS_SECONDS             = 31536000    # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS  = True
    SECURE_HSTS_PRELOAD             = True
    SECURE_REFERRER_POLICY          = "strict-origin-when-cross-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {module} {message}", "style": "{"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO"},
        "myapp": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO"},
    },
}
