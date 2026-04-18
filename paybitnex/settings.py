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
# - WAL journal mode lets readers proceed while a writer is writing.
# - `synchronous=NORMAL` is still crash-safe in WAL mode but much faster
#   than the default FULL.
# - busy_timeout makes Django wait instead of immediately erroring when
#   the database is briefly locked (Celery + web workers can both write).
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["init_command"] = (
        "PRAGMA journal_mode=WAL; "
        "PRAGMA synchronous=NORMAL; "
        "PRAGMA busy_timeout=5000; "
        "PRAGMA foreign_keys=ON; "
    )
    # 20s timeout at the Python sqlite3 driver level — belt-and-suspenders
    # for the occasional write collision.
    DATABASES["default"]["OPTIONS"]["timeout"] = 20

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

# Cloudinary
CLOUDINARY_STORAGE = {
    "CLOUD_NAME": config("CLOUDINARY_CLOUD_NAME", default=""),
    "API_KEY": config("CLOUDINARY_API_KEY", default=""),
    "API_SECRET": config("CLOUDINARY_API_SECRET", default=""),
}
DEFAULT_FILE_STORAGE = "cloudinary_storage.storage.MediaCloudinaryStorage"

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
