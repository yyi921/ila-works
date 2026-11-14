"""The labour ledger: reading Micropay pay-run files, saving pay runs, and checking them against Xero."""

import io
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from core.models import Account, Dimension, Journal, JournalLine, Organisation, TrackingCategory, TrackingOption
from labour import ledger
from labour.micropay import FileError, read
from labour.models import CostAccount, Employee, LabourSettings, PayLine, PayRun

HEADERS = ["Employee Code", "Surname", "Full Name", "Date Of Birth", "Employment Type", "Pay Period ID",
           "Period End Date", "Super Fund Description", "Cost Account Code", "Cost Account Description",
           "Pay Comp/Add Ded Code", "Pay Comp/Add Ded Desc", "Transaction Type", "Leave Start Date", "Leave End Date",
           "Pay Period Pay Frequency", "Hours", "Amount", "Pay Advice Print Date"]
END = datetime(2026, 10, 18)


def row(code, name, cost, cost_name, comp, kind, start, end, hours, amount, period_end=END):
    return [code, name.split()[-1], name, datetime(1990, 1, 2), "CA - Casual", 1460, period_end, "Host Plus", cost,
            cost_name, comp[:6], comp, kind, start, end, "Fortnightly", hours, amount, 46316]  # 46316 = 21/10/2026


ROWS = [
    row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Normal Pay", "Hours By Rate",
        datetime(2026, 10, 10), datetime(2026, 10, 12), 20, 600.00),
    row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Saturday Loading - 50%", "Non Standard Add Before",
        datetime(2026, 10, 10), datetime(2026, 10, 10), 8, 120.00),
    row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Superannuation", "Super",
        datetime(2026, 10, 5), datetime(2026, 10, 18), 0, 86.40),
    row(222, "Bo Chen", "700-2100", "TECC - Fixed Wages", "Auto Pay", "Auto Pay",
        datetime(2026, 10, 5), datetime(2026, 10, 18), 76, 2743.00),
    row(222, "Bo Chen", "700-2100", "TECC - Fixed Wages", "Sick Leave", "Sick Leave",
        46301, 46301, 7.6, 371.11),  # Excel serial dates: 6/10/2026
    row(333, "Cy Dunn", "700-2600", "TECC - Cleaner", "Nauseous allowance", "Non Standard Add Before",
        "16/10/2026", "16/10/2026", 0, 27.63),
    row(333, "Cy Dunn", "700-2600", "TECC - Cleaner", "Other Leave", "Other Leave", "30/12/1899", None, 0, 0),
]
TOTAL = D("3948.14")


def workbook(rows=ROWS, summary_total=TOTAL, sheet="Micropay Breakdown", headers=HEADERS):
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    for r in (["Transaction Type", "(All)"], [], ["Row Labels", "Sum of Amount"], ["TECC - F&B Service", 806.4],
              ["Grand Total", float(summary_total) if summary_total is not None else None]):
        summary.append(r)
    sheet = book.create_sheet(sheet)
    sheet.append(headers)
    for r in rows:
        sheet.append(r)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@pytest.fixture
def org(db):
    user = get_user_model().objects.create_user("finance", password="pw")
    org = Organisation.objects.create(name="TECC")
    org.members.add(user)
    for code, name, kind in [("2350", "Payroll Clearing", "liability"), ("6312", "Labour - Allowances", "expense"),
                             ("6335", "Labour - Payroll Tax", "expense"), ("6345", "Labour - Salaries Event Staff",
                                                                           "expense"),
                             ("6355", "Labour - Sick Leave", "expense"), ("6370", "Labour - Superannuation", "expense"),
                             ("6380", "Labour - Workcover Insurance", "expense"),
                             ("6644", "Cleaning - Contractor", "expense")]:
        Account.objects.create(organisation=org, code=code, name=name, account_class=kind)
    departments = TrackingCategory.objects.create(organisation=org, name="Departments", source_system="xero",
                                                  source_id="tc-dept")
    for n in ("Food and Beverage Services", "Overhead & Venue Costs", "Event Hire & Service Recoveries"):
        TrackingOption.objects.create(organisation=org, category=departments, name=n, source_system="xero",
                                      source_id=f"to-{n[:4]}")
    Dimension.objects.create(organisation=org, code="event", name="Event")
    return org, user


def fne_journal(org, narration="Ville for TECC Payroll Journal FNE: 18/10/2026", day=date(2026, 10, 18)):
    journal = Journal.objects.create(organisation=org, date=day, narration=narration, status=Journal.Status.POSTED,
                                     source_system="xero", source_id=f"j-{narration}")
    accounts = {a.code: a for a in Account.objects.filter(organisation=org)}
    lines = {"6345": "3463.00", "6355": "371.11", "6370": "86.40", "6644": "27.63",
             "6335": "195.43", "6380": "67.55", "2350": "-4211.12"}
    for n, (code, amount) in enumerate(lines.items(), 1):
        JournalLine.objects.create(organisation=org, journal=journal, line_number=n, account=accounts[code],
                                   amount=D(amount))
    return journal


# --- Reading the file -------------------------------------------------------------------------------------------

def test_reads_lines_dates_and_totals():
    run = read(workbook())
    assert (run.pay_period_id, run.period_end, run.pay_date, run.frequency) == (
        "1460", date(2026, 10, 18), date(2026, 10, 21), "Fortnightly")
    assert run.total == TOTAL == run.summary_total and run.period_start == date(2026, 10, 5)
    first, sick, allowance, blank = run.lines[0], run.lines[4], run.lines[5], run.lines[6]
    assert (first.date_from, first.date_to, first.hours, first.amount) == (date(2026, 10, 10), date(2026, 10, 12),
                                                                           D("20.00"), D("600.00"))
    assert (sick.date_from, sick.date_to) == (date(2026, 10, 6), date(2026, 10, 6))
    assert allowance.date_from == date(2026, 10, 16)
    assert (blank.date_from, blank.date_to) == (date(2026, 10, 5), date(2026, 10, 18))  # 1899 = no date: fortnight
    assert "1 line(s) had no dates" in run.warnings[0]
    assert not hasattr(first, "date_of_birth") and "Host Plus" not in repr(run.lines)


def test_summary_that_does_not_tie_is_a_warning():
    run = read(workbook(summary_total=D("100.00")))
    assert any("Grand Total is 100.00" in w for w in run.warnings)


@pytest.mark.parametrize("data, message", [
    (b"not a workbook", "isn't an Excel workbook"),
    (workbook(sheet="Sheet2"), 'no "Micropay Breakdown" sheet'),
    (workbook(headers=[h for h in HEADERS if h != "Amount"]), "missing these columns: Amount"),
    (workbook(rows=ROWS + [row(444, "Di Eve", "700-3000", "F&B", "Normal Pay", "Hours By Rate", None, None, 1, 1,
                               period_end=datetime(2026, 11, 1))]), "more than one Period End Date"),
    (workbook(rows=[]), "no employee lines"),
])
def test_files_that_cannot_be_read(data, message):
    with pytest.raises(FileError, match=message):
        read(data)


# --- Saving -----------------------------------------------------------------------------------------------------

def test_upload_saves_the_run_with_only_code_and_name(org):
    result = ledger.upload(org[0], org[1], "breakdown 18.10.xlsx", workbook())
    run = result.run
    assert (run.total, run.line_count, run.employee_count, run.period_start) == (TOTAL, 7, 3, date(2026, 10, 5))
    assert {c.code for c in result.new_cost_accounts} == {"700-3000", "700-2100", "700-2600"}
    ann = Employee.objects.get(code="111")
    assert (ann.name, ann.employment_type) == ("Ann Lee", "CA - Casual")
    stored = {f.name for f in Employee._meta.fields} | {f.name for f in PayLine._meta.fields}
    assert not {"date_of_birth", "super_fund", "surname"} & stored
    assert PayLine.objects.filter(run=run, employee=ann).count() == 3

    settings = LabourSettings.objects.get(organisation=org[0])
    assert (settings.credit_account, settings.department_tracking, settings.event_dimension.name) == (
        "2350", "Departments", "Event")
    assert [(c.name, c.account, c.rate) for c in settings.on_costs.all()] == [("Payroll tax", "6335", None),
                                                                              ("Workcover", "6380", None)]
    assert [(r.text, r.account) for r in settings.component_rules.all()] == [
        ("Super", "6370"), ("Sick Leave", "6355"), ("allowance", "6312")]


def test_same_file_is_unchanged_and_a_new_file_replaces_the_run(org):
    first = ledger.upload(org[0], org[1], "a.xlsx", workbook())
    again = ledger.upload(org[0], org[1], "a.xlsx", workbook())
    assert again.unchanged and again.run.pk == first.run.pk
    fewer = ledger.upload(org[0], org[1], "b.xlsx", workbook(rows=ROWS[:2], summary_total=D("720.00")))
    assert fewer.replaced and fewer.run.pk == first.run.pk
    run = PayRun.objects.get()
    assert (run.total, run.line_count, run.file_name) == (D("720.00"), 2, "b.xlsx")
    assert PayLine.objects.count() == 2 and not fewer.new_cost_accounts


def test_finds_the_xero_journal_learns_rates_and_compares(org):
    fne_journal(org[0], "Ville for TECC Payroll Journal Accrual - October", date(2026, 10, 18))  # not this one
    journal = fne_journal(org[0])
    result = ledger.upload(org[0], org[1], "a.xlsx", workbook())
    assert result.run.xero_journal == journal
    assert result.learned[0].startswith("Payroll tax rate set to 4.950%, learned from Ville for TECC Payroll "
                                        "Journal FNE: 18/10/2026")
    settings = ledger.settings_for(org[0])
    assert [c.rate for c in settings.on_costs.all()] == [D("0.04950"), D("0.01711")]

    CostAccount.objects.filter(code__in=["700-3000", "700-2100", "700-2600"]).update(gl_account="6345")
    rows, total = ledger.xero_comparison(result.run, settings)
    by_code = {r[0]: r[2:] for r in rows}
    assert by_code["6345"] == (D("3463.00"), D("3463.00"), D("0.00"))
    assert by_code["6312"] == (D("27.63"), D("0"), D("27.63"))  # the allowance rule
    assert by_code["6644"] == (D("0"), D("27.63"), D("-27.63"))  # Xero put the cleaner's allowance in 6644
    assert by_code["6335"] == (D("195.43"), D("195.43"), D("0.00"))
    assert "2350" not in by_code and total == (D("4211.12"), D("4211.12"), D("0.00"))

    CostAccount.objects.filter(code="700-3000").update(gl_account="")
    gl, _ = ledger.by_gl(result.run, settings)
    assert gl[ledger.NOT_MAPPED] == D("720.00")


def test_cost_account_department_must_be_a_xero_department(org):
    from django.core.exceptions import ValidationError

    ledger.settings_for(org[0])
    account = CostAccount(organisation=org[0], code="700-3000", department="Kitchen", gl_account="6345")
    with pytest.raises(ValidationError, match="Choose one of Xero's departments"):
        account.full_clean()
    account.department = "Food and Beverage Services"
    account.full_clean()


# --- Console and admin ------------------------------------------------------------------------------------------

@pytest.fixture
def console(client, org, monkeypatch):
    from console.tests.fake_xero import fake_source

    source = fake_source()
    monkeypatch.setattr("console.views.source_for", lambda organisation: source)
    client.copied = []
    monkeypatch.setattr("labour.console.copy_period",
                        lambda organisation, src, kinds, start, end: client.copied.append((kinds, start, end)))
    client.force_login(org[1])
    return client


def say(client, text, files=()):
    data = client.post("/console/run/", {"command": text, "file": list(files)}).json()
    assert data["ok"], data
    return data["result"]


def test_console_upload_and_list(console, org):
    fne_journal(org[0])
    upload = SimpleUploadedFile("breakdown 18.10.xlsx", workbook())
    result = say(console, "/labour << breakdown 18.10.xlsx", [upload])
    assert result["title"] == "/labour · Saved: pay run ending 18/10/2026"
    assert console.copied == [(("journals",), date(2026, 10, 11), date(2026, 11, 8))]  # Xero's journals read first
    figures = {f["label"]: f["value"] for b in result["blocks"] if b["type"] == "figures" for f in b["items"]}
    assert figures["Days worked"] == "05/10/2026 – 18/10/2026" and figures["Employees"] == "3"
    tables = {b["title"]: b for b in result["blocks"] if b["type"] == "table"}
    assert [r["cells"][0] for r in tables["By cost account"]["rows"]][:3] == [
        "700-2100 TECC - Fixed Wages", "700-2600 TECC - Cleaner", "700-3000 TECC - F&B Service"]
    assert "Against Xero: Ville for TECC Payroll Journal FNE: 18/10/2026" in tables
    assert result["link"].endswith("?mapping=missing")

    listed = say(console, "/labour --Oct 2026")
    rows = [b for b in listed["blocks"] if b["type"] == "table"][0]["rows"]
    assert rows[0]["cells"][0] == "18 Oct 2026" and rows[0]["cells"][5] == "Yes"
    assert say(console, "/labour --Dec 2026")["blocks"][0]["rows"] == []


def test_console_rejects_other_files(console):
    data = console.post("/console/run/", {"command": "/labour << pay.csv",
                                          "file": [SimpleUploadedFile("pay.csv", b"a,b")]}).json()
    assert not data["ok"] and "isn't an Excel workbook" in data["error"]


def test_admin_upload_and_run_page(client, org):
    boss = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(boss)
    assert client.get("/admin/labour/payrun/").status_code == 200
    response = client.post("/admin/labour/payrun/upload/", {
        "organisation": org[0].pk, "file": SimpleUploadedFile("breakdown.xlsx", workbook())})
    run = PayRun.objects.get()
    assert response.status_code == 302 and response["Location"].endswith(f"/admin/labour/payrun/{run.pk}/change/")
    page = client.get(response["Location"]).content.decode()
    assert "By cost account" in page and "700-3000 TECC - F&amp;B Service" in page and "Not in Xero yet" in page
    bad = client.post("/admin/labour/payrun/upload/", {
        "organisation": org[0].pk, "file": SimpleUploadedFile("breakdown.xlsx", b"nope")})
    assert "isn&#x27;t an Excel workbook" in bad.content.decode()
    page = client.get("/admin/labour/costaccount/?mapping=missing").content.decode()
    assert "700-2600" in page


# --- Cost accounts from a CSV -----------------------------------------------------------------------------------

def cost_csv(rows):
    out = io.StringIO()
    import csv

    csv.writer(out).writerows(rows)
    return out.getvalue()


def test_cost_accounts_from_csv_are_checked_then_saved(org):
    from labour.cost_accounts import import_csv, to_csv

    CostAccount.objects.create(organisation=org[0], code="700-2100", name="TECC - Fixed Wages", gl_account="6345")
    bad = import_csv(org[0], org[1], cost_csv([
        ["Code", "Name", "Department", "GL account", "Can go to an event"],
        ["700-3000", "TECC - F&B Service", "Kitchen", "9999", "maybe"],
        ["700-3000", "Again", "", "", ""], ["", "No code", "", "", ""]]))
    assert bad.problems == [
        "Row 2 (700-3000): Kitchen isn't one of Xero's departments (Event Hire & Service Recoveries, Food and "
        "Beverage Services, Overhead & Venue Costs).",
        "Row 2 (700-3000): GL account 9999 isn't in the chart of accounts.",
        "Row 2 (700-3000): Can go to an event must be yes or no, not maybe.",
        "Row 3: 700-3000 is also on row 2.", "Row 4: Code is empty."]
    assert CostAccount.objects.count() == 1  # nothing saved

    good = import_csv(org[0], org[1], "﻿" + cost_csv([
        ["Cost Account Code", "Name", "Department", "GL", "Can go to an event"],
        ["700-3000", "TECC - F&B Service", "food and beverage services", "6345", "yes"],
        ["700-2100", "", "Overhead & Venue Costs", "", "no"]]))
    assert not good.problems and good.created == ["700-3000"] and good.updated == ["700-2100"]
    fb, fixed = CostAccount.objects.get(code="700-3000"), CostAccount.objects.get(code="700-2100")
    assert (fb.department, fb.gl_account, fb.taggable) == ("Food and Beverage Services", "6345", True)
    assert (fixed.name, fixed.gl_account, fixed.taggable) == ("TECC - Fixed Wages", "6345", False)  # blanks kept
    again = import_csv(org[0], org[1], to_csv(org[0]))
    assert again.unchanged == 2 and not again.created and not again.updated
    assert to_csv(org[0]).splitlines()[1] == "700-2100,TECC - Fixed Wages,Overhead & Venue Costs,6345,no"
    assert "Unknown column \"Venue\"" in import_csv(org[0], org[1], "Code,Venue\nA,B\n").problems[0]


def test_admin_cost_account_csv_buttons(client, org):
    boss = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(boss)
    page = client.get("/admin/labour/costaccount/").content.decode()
    assert "Upload cost accounts (CSV)" in page and f"/download/?organisation={org[0].pk}" in page
    csv_text = client.get(f"/admin/labour/costaccount/download/?organisation={org[0].pk}").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0] == "Code,Name,Department,GL account,Can go to an event"
    bad = client.post("/admin/labour/costaccount/upload/", {"organisation": org[0].pk, "file": SimpleUploadedFile(
        "cost.csv", b"Code,Department\n700-3000,Kitchen\n")}).content.decode()
    assert "isn&#x27;t one of Xero&#x27;s departments" in bad and "Nothing was saved" in bad
    response = client.post("/admin/labour/costaccount/upload/", {"organisation": org[0].pk, "file": SimpleUploadedFile(
        "cost.csv", b"Code,Department,GL account\n700-3000,Food and Beverage Services,6345\n")})
    assert response.status_code == 302 and CostAccount.objects.get(code="700-3000").gl_account == "6345"
    wrong = client.post("/admin/labour/costaccount/upload/", {"organisation": org[0].pk, "file": SimpleUploadedFile(
        "cost.xlsx", b"x")}).content.decode()
    assert "isn&#x27;t a CSV file" in wrong
