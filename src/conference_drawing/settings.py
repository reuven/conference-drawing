"""Django settings, configured from the environment and one project-local .env file.

The .env file is read with ``dotenv_values`` so nothing leaks into ``os.environ``,
and only from ``CD_ENV_FILE`` or the project directory -- never from the home
directory.
"""

import os
from pathlib import Path

import django_stubs_ext
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import dotenv_values

# Make django-stubs generics (ModelAdmin[M], ...) subscriptable at runtime.
django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

_file_values = dotenv_values(os.environ.get("CD_ENV_FILE") or BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """Return a setting from the real environment, else the .env file, else default."""
    return os.environ.get(name) or _file_values.get(name) or default


DEBUG = _env("DJANGO_DEBUG").lower() in {"1", "true", "yes", "on"}

SECRET_KEY = _env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DEBUG is off")
    SECRET_KEY = "insecure-development-key-never-use-in-production"  # noqa: S105

ALLOWED_HOSTS = [
    h.strip()
    for h in _env(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]" if DEBUG else ""
    ).split(",")
    if h.strip()
]

DATA_DIR = Path(_env("CD_DATA_DIR") or BASE_DIR / "var")

DRIP_ACCOUNT_ID = _env("DRIP_ACCOUNT_ID")
DRIP_API_KEY = _env("DRIP_API_KEY")

LOGIN_URL = "admin:login"

# Extends ModelBackend; listing both would hash twice on every failed login.
AUTHENTICATION_BACKENDS = ["conference_drawing.auth.EmailOrUsernameBackend"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "conference_drawing.raffle",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "conference_drawing.middleware.DefaultHeadersMiddleware",
    # Turns a blocked @ratelimit view into RATELIMIT_VIEW's response (a 429).
    "django_ratelimit.middleware.RatelimitMiddleware",
]

ROOT_URLCONF = "conference_drawing.urls"
WSGI_APPLICATION = "conference_drawing.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "conference_drawing.ops.version_context",
            ],
        },
    },
]

# PostgreSQL only. On the server the app's OS user maps to the role of the same
# name by peer auth over the unix socket (empty HOST), so no password exists;
# CD_DB_PASSWORD is for a TCP setup such as CI's service container.
# select_for_update() takes real row locks, which is what serializes two phones
# tapping the same pick (see draw.py). ATOMIC_REQUESTS stays off: services open
# their own short transactions, and deletion must VACUUM outside one.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _env("CD_DB_NAME", "conference_drawing"),
        "USER": _env("CD_DB_USER", "conference-drawing"),
        "PASSWORD": _env("CD_DB_PASSWORD"),
        "HOST": _env("CD_DB_HOST"),
        "PORT": _env("CD_DB_PORT"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

# Rate limiting (django-ratelimit) needs a cache shared across gunicorn workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "cd_cache",
    }
}
RATELIMIT_VIEW = "conference_drawing.raffle.views.public.rate_limited"
SILENCED_SYSTEM_CHECKS = ["django_ratelimit.E003", "django_ratelimit.W001"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = DATA_DIR / "static"
MEDIA_URL = "/media/"
MEDIA_ROOT = DATA_DIR / "media"

DATA_UPLOAD_MAX_MEMORY_SIZE = 262_144
FILE_UPLOAD_MAX_MEMORY_SIZE = 5_242_880

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

# TLS terminates at Caddy, which sets X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s %(message)s"}},
    # Display tokens and exception text never reach journald (logredact.py).
    "filters": {"redact": {"()": "conference_drawing.logredact.RedactFilter"}},
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "filters": ["redact"],
        },
    },
    "root": {"handlers": ["stderr"], "level": "INFO"},
}
