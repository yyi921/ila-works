from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.cache import cache
from django.utils import timezone

from core.models import Organisation
from xero.api import XeroAPIError, XeroClient, XeroRateLimited, XeroScopeMissing, parse_date, report_rows
from xero.models import XeroConnection


@pytest.fixture
def connection(db):
    cache.clear()
    return XeroConnection.objects.create(
        organisation=Organisation.objects.create(name="Demo"), tenant_id="11111111-1111-1111-1111-111111111111",
        access_token="at", refresh_token="rt", scopes="accounting.reports.profitandloss.read",
        expires_at=timezone.now() + timedelta(minutes=20),
    )


def response(status, json=None, headers=None):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=json or {}), headers=headers or {})


def test_get_sends_tenant_header_and_caches(connection):
    with mock.patch("xero.api.requests.get", return_value=response(200, {"ok": 1})) as get:
        client = XeroClient(connection)
        assert client.get("Organisation") == {"ok": 1}
        assert client.get("Organisation") == {"ok": 1}
    assert get.call_count == 1
    headers = get.call_args.kwargs["headers"]
    assert headers["xero-tenant-id"] == "11111111-1111-1111-1111-111111111111"
    assert headers["Authorization"] == "Bearer at"


def test_get_all_pages(connection):
    pages = [response(200, {"Invoices": [{}] * 1000}), response(200, {"Invoices": [{}] * 3})]
    with mock.patch("xero.api.requests.get", side_effect=pages) as get:
        assert len(XeroClient(connection).get_all("Invoices", "Invoices")) == 1003
    assert [c.kwargs["params"]["page"] for c in get.call_args_list] == [1, 2]


def test_401_forces_one_token_refresh_then_retries(connection):
    with mock.patch("xero.api.requests.get", side_effect=[response(401), response(200, {"ok": 1})]), \
            mock.patch("xero.oauth._token_request", return_value={
                "access_token": "new", "refresh_token": "rt2", "expires_at": timezone.now() + timedelta(minutes=30)}):
        assert XeroClient(connection).get("Organisation") == {"ok": 1}
    connection.refresh_from_db()
    assert connection.access_token == "new" and connection.refresh_token == "rt2"


def test_repeated_401_or_403_asks_to_reconnect(connection):
    with mock.patch("xero.api.requests.get", return_value=response(403)):
        with pytest.raises(XeroScopeMissing, match="/connect"):
            XeroClient(connection).get("Reports/BalanceSheet")


def test_rate_limit_message(connection):
    with mock.patch("xero.api.requests.get",
                    return_value=response(429, headers={"Retry-After": "42", "X-Rate-Limit-Problem": "minute"})):
        with pytest.raises(XeroRateLimited, match="42 seconds"):
            XeroClient(connection).get("Invoices")


def test_revoked_connection(connection):
    connection.status = XeroConnection.Status.REVOKED
    connection.save()
    with pytest.raises(XeroAPIError, match="disconnected"):
        XeroClient(connection).get("Invoices")


def test_require_scope(connection):
    client = XeroClient(connection)
    client.require_scope("accounting.reports.profitandloss.read")
    with pytest.raises(XeroScopeMissing, match="Balance Sheet"):
        client.require_scope("accounting.reports.balancesheet.read")


def test_parse_date_formats():
    assert parse_date("2026-09-30T00:00:00") == date(2026, 9, 30)
    assert parse_date("/Date(1790726400000+0000)/") == date(2026, 9, 30)
    assert parse_date("") is None


def test_report_rows_flattens_sections():
    report = {"Reports": [{"Rows": [
        {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "30 Sep 2026"}]},
        {"RowType": "Section", "Title": "Income", "Rows": [
            {"RowType": "Row", "Cells": [{"Value": "Sales", "Attributes": [{"Id": "account", "Value": "a1"}]},
                                         {"Value": "10.00"}]},
            {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "10.00"}]},
        ]},
    ]}]}
    header, rows = report_rows(report)
    assert header == ["", "30 Sep 2026"]
    assert rows[0] == {"section": "Income", "type": "Row", "label": "Sales", "account_id": "a1", "values": ["10.00"]}
    assert rows[1]["type"] == "SummaryRow" and rows[1]["account_id"] is None
