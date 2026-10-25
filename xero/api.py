"""Read-only client for Xero's Accounting API: one tenant per client, short-lived response cache, paging, and
errors worded for the console. Tokens must never be logged or put in exception messages."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

from . import oauth

logger = logging.getLogger(__name__)

BASE_URL = "https://api.xero.com/api.xro/2.0/"
TIMEOUT = 30
PAGE_SIZE = 1000
CACHE_SECONDS = 300


class XeroAPIError(Exception):
    """Message is safe to show to the user."""


class XeroRateLimited(XeroAPIError):
    pass


class XeroScopeMissing(XeroAPIError):
    pass


SCOPE_LABELS = {
    "accounting.reports.balancesheet.read": "Balance Sheet report",
    "accounting.reports.banksummary.read": "Bank Summary report",
    "accounting.reports.aged.read": "aged receivables and payables reports",
    "accounting.budgets.read": "budgets",
    "accounting.attachments.read": "attachments (bill PDFs)",
}


class XeroClient:
    def __init__(self, connection):
        self.connection = connection

    def require_scope(self, scope):
        if scope not in (self.connection.scopes or "").split():
            raise XeroScopeMissing(
                f"iLA doesn't yet have permission to read {SCOPE_LABELS.get(scope, scope)} from Xero. "
                "Reconnect with /connect to grant it."
            )

    def has_scope(self, scope):
        return scope in (self.connection.scopes or "").split()

    def _cache_key(self, path, params):
        digest = hashlib.sha256(json.dumps([path, params], sort_keys=True, default=str).encode()).hexdigest()
        return f"xero:{self.connection.tenant_id}:{digest}"

    def get(self, path, params=None, *, cache_seconds=CACHE_SECONDS):
        params = params or {}
        key = self._cache_key(path, params)
        if cache_seconds and (hit := cache.get(key)) is not None:
            return hit
        data = self._request(path, params)
        if cache_seconds:
            cache.set(key, data, cache_seconds)
        return data

    def get_all(self, path, collection, params=None, *, cache_seconds=CACHE_SECONDS):
        """Every record of a paged endpoint (Invoices, BankTransactions, Payments, Contacts...)."""
        rows, page = [], 1
        while True:
            batch = self.get(path, {**(params or {}), "page": page, "pageSize": PAGE_SIZE},
                             cache_seconds=cache_seconds).get(collection, [])
            rows += batch
            if len(batch) < PAGE_SIZE:
                return rows
            page += 1

    def get_file(self, path):
        """A file from Xero (an attachment): (content bytes, content type). Never cached."""
        return self._request(path, {}, raw=True)

    def _request(self, path, params, *, retried=False, raw=False):
        try:
            token = oauth.get_access_token(self.connection)
        except oauth.XeroConnectionRevoked:
            raise XeroAPIError("The Xero connection has been disconnected. Reconnect with /connect.") from None
        except oauth.XeroOAuthError:
            raise XeroAPIError("Couldn't refresh the Xero connection. Try again in a minute.") from None
        try:
            resp = requests.get(
                BASE_URL + path, params=params, timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {token}", "xero-tenant-id": str(self.connection.tenant_id),
                         "Accept": "*/*" if raw else "application/json"},
            )
        except requests.RequestException:
            logger.exception("Xero request to %s failed", path)
            raise XeroAPIError("Couldn't reach Xero. Try again in a minute.") from None

        if resp.status_code == 200:
            return (resp.content, resp.headers.get("Content-Type", "")) if raw else resp.json()
        if resp.status_code == 401 and not retried:
            # Could be an access token Xero has already invalidated: force one refresh, then retry. If the refresh
            # token is rejected, get_access_token marks the connection revoked.
            type(self.connection).objects.filter(pk=self.connection.pk).update(expires_at=datetime(2000, 1, 1, tzinfo=dt_timezone.utc))
            self.connection.refresh_from_db(fields=["expires_at"])
            return self._request(path, params, retried=True, raw=raw)
        if resp.status_code in (401, 403):
            raise XeroScopeMissing("Xero refused access to this data. Reconnect with /connect to re-authorise iLA.")
        if resp.status_code == 429:
            wait = resp.headers.get("Retry-After", "60")
            problem = resp.headers.get("X-Rate-Limit-Problem", "minute")
            raise XeroRateLimited(f"Xero's {problem} rate limit is reached. Try again in {wait} seconds.")
        if resp.status_code == 404:
            raise XeroAPIError("Xero couldn't find that record.")
        logger.warning("Xero %s returned %s", path, resp.status_code)
        raise XeroAPIError(f"Xero returned an error ({resp.status_code}). Try again shortly.")


# --- Parsing helpers --------------------------------------------------------------------------------------------

_MS_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def parse_date(value):
    """Xero dates come as "2026-09-30T00:00:00" (the *DateString fields) or "/Date(1790726400000+0000)/"."""
    if not value:
        return None
    m = _MS_DATE.fullmatch(value)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc).date()
    return datetime.fromisoformat(value[:19]).date()


def record_date(record, field="Date"):
    return parse_date(record.get(f"{field}String") or record.get(field))


def to_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def report_rows(report):
    """Flatten a Xero report into rows: dicts with ``section`` (title), ``type`` (Row or SummaryRow), ``label``,
    ``account_id`` (or None) and ``values`` (the remaining cells as strings). Also returns the header cells."""
    header, rows = [], []

    def walk(items, section):
        for row in items:
            kind = row.get("RowType")
            if kind == "Header":
                header[:] = [c.get("Value", "") for c in row.get("Cells", [])]
            elif kind == "Section":
                walk(row.get("Rows", []), row.get("Title") or section)
            elif kind in ("Row", "SummaryRow"):
                cells = row.get("Cells", [])
                account_id = None
                for cell in cells:
                    for attr in cell.get("Attributes") or []:
                        if attr.get("Id") == "account":
                            account_id = attr.get("Value")
                rows.append({
                    "section": section or "", "type": kind, "label": cells[0].get("Value", "") if cells else "",
                    "account_id": account_id, "values": [c.get("Value", "") for c in cells[1:]],
                })

    report = report["Reports"][0] if "Reports" in report else report
    walk(report.get("Rows", []), "")
    return header, rows
