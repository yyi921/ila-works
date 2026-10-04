# ila-works
This is multi-systems control center like retro DOS window

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate elsewhere
pip install -r requirements-dev.txt
cp .env.example .env                             # then fill in real values
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
