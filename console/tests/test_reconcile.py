from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from console.models import ReconSession
from core.models import Account, AuditLog, Organisation, ReconMethod, Reconciliation
from xero.api import XeroClient
from xero.source import XeroSource
from xero.sync import proposed_method, refresh_accounts

from .fake_xero import fake_source

D = Decimal
EXTRA_ACCOUNTS = [
    {"AccountID": "a-clear", "Code": "820", "Name": "Wages Clearing", "Type": "CURRLIAB", "Status": "ACTIVE"},
    {"AccountID": "a-prepay", "Code": "1400", "Name": "Prepayments", "Type": "CURRENT", "Status": "ACTIVE"},
    {"AccountID": "a-accr", "Code": "2000", "Name": "Accrued Expenses", "Type": "CURRLIAB", "Status": "ACTIVE"},
    {"AccountID": "a-loan", "Code": "900", "Name": "Bank Loan", "Type": "TERMLIAB", "Status": "ACTIVE"},
    {"AccountID": "a-re", "Code": "960", "Name": "Retained Earnings", "Type": "EQUITY",
     "SystemAccount": "RETAINEDEARNINGS", "Status": "ACTIVE"},
]
BALANCES = {"a-bank": "15000.00", "a-save": "0.00", "a-ar": "7300.00", "a-ap": "2200.00", "a-clear": "35.00", "a-prepay": "8587.50", "a-accr": "1500.00",
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


def say(client, text, files=()):
    data = client.post("/console/run/", {"command": text, "file": list(files)}).json()
    assert data["ok"], data
    return data["result"]


def error(client, text, files=()):
    data = client.post("/console/run/", {"command": text, "file": list(files)}).json()
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
    assert figures(result)["To do"] == "8"
    assert Account.objects.filter(organisation=org[0]).count() == 16


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


# --- Schedules ------------------------------------------------------------------------------------------------------

SCHEDULE = ("Account,Description,Reference,Amount,From,To,Notes\n"
            '1400,Public liability insurance,INV-5521,"6,187.50",01/07/2026,30/06/2027,9 of 12 left\n'
            '1400,Ticketing licence,TS-2026,"2,400.00",01/01/2026,31/12/2026,3 of 12 left\n'
            '900,Bank loan,Loan 4471,"50,000.00",,,Per lender statement\n')


def csv_file(text, name="schedules.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def test_template(client):
    result = say(client, "/reconcile --template")
    assert result["link"] == "/console/reconcile/template/"
    response = client.get("/console/reconcile/template/")
    assert response.content.decode("utf-8-sig").startswith("Account,Description,Reference,Amount,From,To,Notes")


def test_schedules_for_several_accounts(client, org):
    result = say(client, "/reconcile --Sep 2026 << schedules.csv", [csv_file(SCHEDULE)])
    summary = rows(result)
    assert summary["1400"][2:4] == ["2", "8,587.50"] and summary["1400"][6] == "0.00"
    assert summary["900"][3] == "50,000.00"
    r = rec(org, "1400")
    assert r.support_balance == D("8587.50") and r.schedule[0]["reference"] == "INV-5521"
    assert r.schedule_source["file"] == "schedules.csv" and len(r.schedule_source["sha256"]) == 64
    assert AuditLog.objects.filter(input__contains="schedule uploaded").count() == 2
    assert rows(say(client, "/reconcile --Sep 2026"))["1400"][7] == "Ready to sign"


def test_schedule_for_one_account_opens_its_working_paper(client, org):
    text = "Description,Amount,To\nInsurance,6187.50,30/06/2027\nOld licence,2400,31/08/2026\n"
    result = say(client, "/reconcile 1400 --Sep 2026 << prepay.csv", [csv_file(text, "prepay.csv")])
    assert result["title"] == "1400 Prepayments at 30 Sep 2026"
    assert tables(result)[0]["title"] == "Schedule from prepay.csv"
    assert "ended by 30 Sep 2026 still have a balance: 2 Old licence" in notes(result)
    say(client, "prepared")
    result = say(client, "<< prepay.csv", [csv_file("Description,Amount\nInsurance,8587.50\n", "prepay.csv")])
    assert "sign-off was cleared" in notes(result)
    assert rec(org, "1400").schedule == [{"description": "Insurance", "reference": "", "amount": "8587.50",
                                         "from": "", "to": "", "notes": ""}]
    result = say(client, "support 8587.50")
    assert "schedule was removed" in notes(result) and rec(org, "1400").schedule == []


def test_schedule_problems_save_nothing(client, org):
    text = ("Account,Description,Amount,From,To\n1400,Insurance,abc,,\n610,Debtors,100,,\n"
            "1400,,5,,\n1400,Licence,5,01/09/2026,01/08/2026\n999,Nope,5,,\n")
    message = error(client, "/reconcile --Sep 2026 << bad.csv", [csv_file(text, "bad.csv")])
    assert "nothing was saved" in message
    assert "row 2:" in message and "isn't an amount" in message
    assert "row 3: 610 Accounts Receivable is reconciled by Debtors/creditors ledger" in message
    assert "row 4: Description is empty" in message and "row 5: To is before From" in message
    assert "row 6: account 999" in message
    assert not Reconciliation.objects.exists()


def test_schedule_needs_account_column_or_account(client):
    message = error(client, "/reconcile --Sep 2026 << s.csv", [csv_file("Description,Amount\nX,1\n", "s.csv")])
    assert 'needs an "Account" column' in message
    message = error(client, "/reconcile 1400 --Sep 2026 << s.csv",
                    [csv_file("Account,Description,Amount\n900,Loan,1\n", "s.csv")])
    assert "account 900 isn't 1400 Prepayments" in message


def test_schedule_not_carried_forward(client, org):
    say(client, "/reconcile 1400 --Aug 2026 << p.csv", [csv_file("Description,Amount\nInsurance,9000\n", "p.csv")])
    result = say(client, "/reconcile 1400 --Sep 2026")
    assert rec(org, "1400").schedule == [] and rec(org, "1400").support_balance is None
    assert "Upload the schedule with /reconcile 1400 --Sep 2026 << schedule.csv" in notes(result)


# --- Accruals from the Expectation Ledger ----------------------------------------------------------------------------


@pytest.fixture
def accrual_journal(client, org):
    from accounting.models import Accrual, Expectation, LedgerJournal, State

    say(client, "/reconcile --Sep 2026")  # loads the chart
    accrual = Accrual.objects.create(organisation=org[0], account_codes="2000, 2010")
    journal = LedgerJournal.objects.create(organisation=org[0], accrual=accrual, period_end=date(2026, 9, 30),
                                           narration="Accruals September 2026")
    power = Account.objects.get(organisation=org[0], code="445")
    for description, amount, state in [("Electricity September", "1000.00", State.POSTED),
                                       ("Cleaning September", "500.00", State.CLOSED),
                                       ("Security September", "300.00", State.PREDICTED)]:
        Expectation.objects.create(organisation=org[0], journal=journal, description=description, account=power,
                                   expected_amount=D(amount), state=state, basis="Run-rate")
    return journal


def test_accrual_account_takes_posted_lines_from_the_expectation_ledger(client, org, accrual_journal):
    result = say(client, "/reconcile 2000 --Sep 2026")
    assert tables(result)[0]["title"] == "Schedule from the Expectation Ledger: Accruals September 2026"
    assert [r["cells"][1] for r in tables(result)[0]["rows"]] == ["Electricity September", "Cleaning September",
                                                                  "Total"]
    assert figures(result)["Supporting balance"] == "1,500.00" and figures(result)["Difference"] == "0.00"
    assert "1 more line(s) in that journal, 300.00, aren't marked posted" in notes(result)
    assert rec(org, "2000").schedule_source["ledger"] == accrual_journal.pk


def test_expectation_ledger_changes_follow_until_signed(client, org, accrual_journal):
    from accounting.models import State

    say(client, "/reconcile 2000 --Sep 2026")
    accrual_journal.lines.filter(state=State.PREDICTED).update(state=State.POSTED)
    result = say(client, "/reconcile 2000 --Sep 2026")
    assert figures(result)["Supporting balance"] == "1,800.00" and figures(result)["Difference"] == "(300.00)"
    say(client, "add -300 Security accrual posted in October")
    say(client, "prepared")
    accrual_journal.lines.filter(description__startswith="Cleaning").delete()
    assert rec(org, "2000").support_balance == D("1800.00")
    say(client, "/reconcile --Sep 2026")
    assert rec(org, "2000").support_balance == D("1800.00") and rec(org, "2000").prepared_at  # signed: frozen


def test_upload_or_typed_support_replaces_the_expectation_ledger(client, org, accrual_journal):
    say(client, "/reconcile 2000 --Sep 2026 << a.csv", [csv_file("Description,Amount\nAll accruals,1500\n", "a.csv")])
    say(client, "/reconcile 2000 --Sep 2026")
    assert rec(org, "2000").schedule_source["file"] == "a.csv"
    say(client, "support 1400")
    say(client, "/reconcile 2000 --Sep 2026")
    r = rec(org, "2000")
    assert r.support_balance == D("1400") and r.schedule == [] and r.schedule_source == {}


def test_only_the_account_accruals_post_to_is_linked(client, org, accrual_journal):
    say(client, "/reconcile 900 --Sep 2026")
    assert rec(org, "900").schedule_source == {}
    accrual_journal.accrual.account_codes = "2010, 2000"
    accrual_journal.accrual.save()
    result = say(client, "/reconcile 2000 --Sep 2026")
    assert rec(org, "2000").support_balance is None
    assert "Upload the schedule" in notes(result)

