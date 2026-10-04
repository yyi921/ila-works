"""Test settings: dummy secrets, and SQLite unless POSTGRES_DB is set (e.g. in CI with a Postgres service)."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-secret")
os.environ.setdefault("XERO_CLIENT_ID", "test-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-client-secret")
_use_postgres = "POSTGRES_DB" in os.environ
for _var in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_var, "unused")

from .settings import *  # noqa: E402,F403

if not _use_postgres:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
XERO_REDIRECT_URI = "http://testserver/xero/callback/"
