"""Test settings: dummy secrets, and SQLite unless POSTGRES_DB is set (e.g. in CI with a Postgres service)."""

import os

from cryptography.fernet import Fernet

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-secret")
os.environ.setdefault("XERO_CLIENT_ID", "test-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIELD_ENCRYPTION_KEYS", Fernet.generate_key().decode())
_use_postgres = "POSTGRES_DB" in os.environ
for _var in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_var, "unused")

from .settings import *  # noqa: E402,F403

if not _use_postgres:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

SECURE_SSL_REDIRECT = False
WHITENOISE_AUTOREFRESH = True  # serve from finders; no collectstatic needed in tests
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
XERO_REDIRECT_URI = "http://testserver/xero/callback/"
