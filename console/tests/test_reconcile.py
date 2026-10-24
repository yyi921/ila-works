from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from console.models import ReconSession
from core.models import Account, AuditLog, Organisation, ReconMethod, Reconciliation
from xero.api import XeroClient
from xero.source import XeroSource
from xero.sync import proposed_method, refresh_accounts

from .fake_xero import fake_source

D = Decimal
EXTRA_ACCOUNTS = [
    {"AccountID": "a-clear", "Code": "820", "Name": "Wages Clearing", "Type": "CURRLIAB", "Status": "ACTIVE"},
    {"AccountID": "a-loan", "Code": "900", "Name": "Bank Loan", "Type": "TERMLIAB", "Status": "ACTIVE"},
    {"AccountID": "a-re", "Code": "960", "Name": "Retained Earnings", "Type": "EQUITY",
     "SystemAccount": "RETAINEDEARNINGS", "Status": "ACTIVE"},
]
BALANCES = {"a-bank": "15000.00", "a-save": "0.00", "a-ar": "7300.00", "a-ap": "2200.00", "a-clear": "35.00",
            "a-loan": "50000.00", "a-re": "1000.00"}


@pytest.fixture
def org(db):
    user = get_user_model().objects.create_user("finance", password="pw")
    org = Organisation.objects.create(name="Demo Events")
    org.members.add(user)
    return org, user


@pytest.fixture
def client(client, org):
    client.force_login(org[1])
    return client


@pytest.fixture
def balances():
    return dict(BALANCES)


@pytest.fixture(autouse=True)
def source(monkeypatch, balances):
    source = fake_source()
    plain = source.raw_accounts
    source.raw_accounts = lambda fresh=False: plain() + EXTRA_ACCOUNTS
    names = {a["AccountID"]: a["Name"] for a in EXTRA_ACCOUNTS}
    source.balance_sheet = lambda as_at: [
        {"section": "", "label": names.get(k, k), "account_id": k, "amount": D(v), "is_total": False}
        for k, v in balances.items()]
    source.ledger_balance = lambda ledger, as_at: {
        "total": D("7300.00") if ledger == "receivable" else D("2000.00"), "documents": 3}
    monkeypatch.setattr("console.views.source_for", lambda organisation: source)
    return source


def say(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert data["ok"], data
    return data["result"]


def error(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert not data["ok"], data
    return data["error"]


def tables(result):
    return [b for b in result["blocks"] if b["type"] == "table"]


def rows(result, index=0):
    return {r["cells"][0]: r["cells"] for r in tables(result)[index]["rows"]}


def figures(result):
    return {f["label"]: f["value"] for b in result["blocks"] if b["type"] == "figures" for f in b["items"]}


def notes(result):
    return " ".join(b["text"] for b in result["blocks"] if b["type"] == "note")


def rec(org, code, period_end=date(2026, 9, 30)):
    return Reconciliation.objects.get(organisation=org[0], account__code=code, period_end=period_end)


# --- Chart of accounts --------------------------------------------------------------------------------------------


def test_proposed_methods():
    assert proposed_method({"Type": "BANK", "Name": "Business Bank"}) == ReconMethod.BANK
    assert proposed_method({"Type": "CURRENT", "SystemAccount": "DEBTORS", "Name": "AR"}) == ReconMethod.SUBLEDGER
    assert proposed_method({"Type": "CURRLIAB", "SystemAccount": "GST", "Name": "GST"}) == ReconMethod.GST
    assert proposed_method({"Type": "CURRLIAB", "Name": "Wages Clearing"}) == ReconMethod.NIL
    assert proposed_method({"Type": "CURRENT", "Name": "Suspense"}) == ReconMethod.NIL
    assert proposed_method({"Type": "CURRENT", "Name": "Intercompany loan - TECC"}) == ReconMethod.INTERCOMPANY
    assert proposed_method({"Type": "FIXED", "Name": "Office Equipment"}) == ReconMethod.FIXED_ASSETS
    assert proposed_method({"Type": "EQUITY", "Name": "Retained Earnings"}) == ReconMethod.NONE
    assert proposed_method({"Type": "CURRENT", "Name": "Prepayments"}) == ReconMethod.SCHEDULE
    assert proposed_method({"Type": "EXPENSE", "Name": "Rent"}) == ""


def test_refresh_keeps_admin_settings(org, source):
    refresh_accounts(org[0], source)
    loan = Account.objects.get(organisation=org[0], code="900")
    assert loan.recon_method == ReconMethod.SCHEDULE
    loan.recon_method, loan.recon_threshold = ReconMethod.NIL, D("1000")
    loan.save()
    source.raw_accounts = lambda fresh=False: [a for a in EXTRA_ACCOUNTS if a["Code"] == "900"]
    result = refresh_accounts(org[0], source)
    loan.refresh_from_db()
    assert (loan.recon_method, loan.recon_threshold) == (ReconMethod.NIL, D("1000"))
    assert result.archived == len(Account.objects.filter(organisation=org[0])) - 1
    assert not Account.objects.get(organisation=org[0], code="090").is_active


# --- The pack -----------------------------------------------------------------------------------------------------


def test_pack_loads_chart_and_lists_accounts(client, org):
    result = say(client, "/reconcile --Sep 2026")
    assert result["title"] == "Balance sheet reconciliation at 30 Sep 2026"
    pack = rows(result)
    assert pack["820"][2] == "Should be nil" and pack["820"][7] == "Difference" and pack["820"][6] == "35.00"
    assert pack["090"][7] == "Not started" and pack["091"][7] == "Not started"  # bank listed even at nil
    assert pack["900"][2] == "Schedule"
    assert "960" not in pack  # equity: not reconciled
    assert "610" in pack and "800" in pack
    assert figures(result)["To do"] == "6"
    assert Account.objects.filter(organisation=org[0]).count() == 14


def test_threshold_and_missing_method(client, org):
    say(client, "/reconcile --Sep 2026")
    Account.objects.filter(organisation=org[0], code="900").update(recon_threshold=D("100000"))
    Account.objects.filter(organisation=org[0], code="610").update(recon_method="")
    result = say(client, "/reconcile --Sep 2026")
    assert rows(result)["900"][7] == "Below threshold"
    assert rows(result)["610"][7] == "Method not set"
    assert "no reconciliation method" in notes(result)


def test_period_must_have_ended(client):
    assert "hasn't ended yet" in error(client, "/reconcile --Dec 2099")


def test_recon_alias_and_bank_status(client):
    assert say(client, "/recon --bank")["title"].startswith("Bank reconciliation at")
    assert say(client, "/rec --Sep 2026")["title"] == "Balance sheet reconciliation at 30 Sep 2026"


# --- Working papers -----------------------------------------------------------------------------------------------


def test_bank_working_paper_prepare_and_self_review(client, org):
    result = say(client, "/reconcile 090 --Sep 2026")
    assert result["title"] == "090 Business Bank at 30 Sep 2026"
    items = tables(result)[0]["rows"]
    assert [r["cells"][4] for r in items] == ["(120.00)", "500.00", "(80.00)", "300.00"]  # unreconciled to 30 Sep
    assert "supporting balance first" in error(client, "prepared")
    result = say(client, "statement 14,700")
    assert figures(result)["Difference"] == "0.00"
    say(client, "prepared")
    result = say(client, "reviewed")
    assert "self-reviewed" in notes(result)
    r = rec(org, "090")
    assert r.prepared_by == org[1] and r.self_reviewed
    assert AuditLog.objects.filter(target_type="core.reconciliation", target_id=str(r.pk)).count() == 2
    assert rows(say(client, "/reconcile --Sep 2026"))["090"][7] == "Reviewed"


def test_difference_blocks_signing_until_explained(client, org):
    say(client, "/reconcile 900 --Sep 2026")
    say(client, "support 49,750")
    assert "difference of 250.00" in error(client, "prepared")
    result = say(client, "add 30/09/2026 250 Interest charged after statement date")
    assert figures(result)["Difference"] == "0.00"
    say(client, "prepared")
    result = say(client, "add 10 Something else")
    assert "sign-off was cleared" in notes(result)
    assert rec(org, "900").prepared_at is None


def test_items_from_xero_cannot_be_removed(client, org):
    say(client, "/reconcile 090 --Sep 2026")
    say(client, "add -45.50 Bank fee not in Xero")
    assert "can't be removed" in error(client, "remove 1")
    result = say(client, "remove 4")
    assert len(rec(org, "090").items) == 3
    assert "Removed 1 item" in notes(result)


def test_xero_balance_change_clears_signoff(client, org, balances):
    say(client, "/reconcile 900 --Sep 2026")
    say(client, "support 50000")
    say(client, "prepared")
    balances["a-loan"] = "49000.00"
    result = say(client, "/reconcile --Sep 2026")
    assert rows(result)["900"][7] == "Difference"
    r = rec(org, "900")
    assert r.prepared_at is None and r.gl_balance == D("49000.00")
    assert AuditLog.objects.filter(input__contains="Xero balance changed").exists()


def test_subledger_uses_ledger(client, org):
    result = say(client, "/reconcile 800 --Sep 2026")
    assert figures(result)["Ledger balance"] == "2,000.00" and figures(result)["Difference"] == "200.00"
    assert "comes from Xero" in error(client, "support 2200")


def test_items_roll_forward(client, org):
    say(client, "/reconcile 900 --Aug 2026")
    say(client, "support 49,900")
    say(client, "add 100 Accrued interest")
    say(client, "note Per lender statement")
    result = say(client, "/reconcile 900 --Sep 2026")
    assert "1 reconciling item(s) brought forward" in notes(result)
    r = rec(org, "900")
    assert r.items[0]["source"] == "carried" and r.items[0]["carried_from"] == "2026-08-31"
    assert r.notes == "Per lender statement" and r.support_balance is None


def test_next_and_done(client, org):
    say(client, "/reconcile 820 --Sep 2026")
    result = say(client, "next")
    assert result["title"].startswith("090") or result["title"].startswith("091")
    result = say(client, "done")
    assert result["title"].startswith("Working paper closed")
    assert not ReconSession.objects.filter(status=ReconSession.Status.OPEN).exists()


def test_profit_and_loss_account_refused(client):
    assert "profit and loss account" in error(client, "/reconcile 200 --Sep 2026")


# --- Debtors and creditors as at a date -----------------------------------------------------------------------------


def _ms(day):
    return f"/Date({int((day - date(1970, 1, 1)).total_seconds()) * 1000}+0000)/"


INVOICE_A = {"InvoiceID": "inv-a", "Type": "ACCREC", "Status": "AUTHORISED", "Date": _ms(date(2026, 8, 1)),
             "AmountDue": "400.00", "CurrencyRate": "1"}
INVOICE_B = {"InvoiceID": "inv-b", "Type": "ACCREC", "Status": "PAID", "Date": _ms(date(2026, 8, 10)),
             "AmountDue": "0.00", "CurrencyRate": "1"}
INVOICE_LATE = {"InvoiceID": "inv-late", "Type": "ACCREC", "Status": "PAID", "Date": _ms(date(2026, 9, 2)),
                "AmountDue": "0.00", "CurrencyRate": "1"}


def _ledger_route(path, params):
    if path == "Invoices" and "IDs" in params:
        wanted = params["IDs"].split(",")
        return {"Invoices": [i for i in (INVOICE_A, INVOICE_B, INVOICE_LATE) if i["InvoiceID"] in wanted]}
    if path == "Invoices":
        assert 'Status=="AUTHORISED"' in params["where"] and "Date<=DateTime(2026,8,31)" in params["where"]
        return {"Invoices": [INVOICE_A]}
    if path == "CreditNotes":
        # Dated 6 Sep, but its allocation to invoice B is dated 20 Aug: Xero counts it from 6 Sep.
        return {"CreditNotes": [{"CreditNoteID": "cn-1", "Type": "ACCRECCREDIT", "Status": "PAID",
                                 "Date": _ms(date(2026, 9, 6)), "RemainingCredit": "0", "CurrencyRate": "1",
                                 "Allocations": [{"Amount": "200.00", "Date": _ms(date(2026, 8, 20)),
                                                  "Invoice": {"InvoiceID": "inv-b"}}]}]}
    if path == "Overpayments":
        return {"Overpayments": [{"OverpaymentID": "op-1", "Type": "RECEIVE-OVERPAYMENT", "Status": "AUTHORISED",
                                  "Date": _ms(date(2026, 8, 15)), "RemainingCredit": "50.00", "CurrencyRate": "1",
                                  "Allocations": [{"Amount": "30.00", "Date": _ms(date(2026, 9, 3)),
                                                   "Invoice": {"InvoiceID": "inv-late"}}]}]}
    if path == "Prepayments":
        return {"Prepayments": []}
    if path == "Payments":
        assert params["where"] == "Date>DateTime(2026,8,31)"
        return {"Payments": [
            {"Status": "AUTHORISED", "Amount": "600.00", "Date": _ms(date(2026, 9, 5)), "Invoice": {"InvoiceID": "inv-a"}},
            {"Status": "DELETED", "Amount": "999.00", "Date": _ms(date(2026, 9, 6)), "Invoice": {"InvoiceID": "inv-a"}},
            {"Status": "AUTHORISED", "Amount": "70.00", "Date": _ms(date(2026, 9, 4)),
             "Invoice": {"InvoiceID": "inv-late"}},
        ]}
    raise AssertionError(f"unexpected call {path} {params}")


class LedgerClient(XeroClient):
    def __init__(self):
        super().__init__(SimpleNamespace(scopes="", tenant_id="ledger-test"))

    def _request(self, path, params, *, retried=False):
        return _ledger_route(path, params)


def test_ledger_balance_works_back_from_today(db):
    source = XeroSource(connection=None)
    source.client = LedgerClient()
    result = source.ledger_balance("receivable", date(2026, 8, 31))
    # A: 400 open + 600 paid after; B: 200 credited from 6 Sep; overpayment open 50 + 30 used after; the
    # September invoice and its payment don't count.
    assert result["total"] == D("400") + D("600") + D("200") - D("80")
    assert result["documents"] == 2
