# ila-works
This is multi-systems control center like retro DOS window

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate elsewhere
pip install -r requirements-dev.txt
cp .env.example .env                             # then fill in real values
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # put in FIELD_ENCRYPTION_KEYS
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Register `http://localhost:8000/xero/callback/` as the redirect URI on your Xero app, log in at `/admin/`, then open `/xero/` to connect.

## Tests

```bash
pytest
```

Tests use in-memory SQLite unless `POSTGRES_DB` is set, in which case they run against Postgres.

## Secrets at rest

OAuth tokens are encrypted with the Fernet keys in `FIELD_ENCRYPTION_KEYS` (newest first). To rotate: prepend a new
key, run `python manage.py rotate_encryption_keys`, then remove the old key. Back the keys up separately from
database backups; without them, stored tokens can't be read and organisations must reconnect Xero.
