import copy

import pytest
from django.contrib.auth import get_user_model

from console.models import TagRun
from core.models import (
    Assignment,
    BankTransaction,
    Dimension,
    DimensionValue,
    Document,
    Journal,
    Organisation,
    Tag,
    TrackingCategory,
    TrackingOption,
)
from xero.sync import copy_period

from .fake_xero import fake_source


def line(desc, account, amount, tracking=None, line_id=None):
    item = {"Description": desc, "AccountCode": account, "LineAmount": amount, "TaxType": "INPUT"}
    if tracking:
        item["Tracking"] = [{"Name": "Event", "Option": tracking}]
    if line_id:
        item["LineItemID"] = line_id
    return item


def doc(doc_id, kind, status, day, contact, lines, reference="", attachments=False):
    return {"InvoiceID": doc_id, "InvoiceNumber": doc_id.upper(), "Type": kind, "Status": status,
            "DateString": f"{day}T00:00:00", "Contact": {"ContactID": "c-" + contact.lower(), "Name": contact},
            "Reference": reference, "LineAmountTypes": "Exclusive", "HasAttachments": attachments,
            "Total": str(sum(float(li["LineAmount"]) for li in lines)), "LineItems": lines}


INVOICES = [
    doc("b1", "ACCPAY", "AUTHORISED", "2026-03-29", "Sound Co", [line("PA hire for the ABBA show", "310", "800.00",
                                                                     line_id="b1-1")], attachments=True),
    doc("i1", "ACCREC", "PAID", "2026-03-14", "Ticketek", [line("Ticket sales", "200", "5000.00", "Celtic Nights",
                                                                 line_id="i1-1")]),
    doc("b2", "ACCPAY", "AUTHORISED", "2026-03-30", "Cleaners", [line("Cleaning", "412", "300.00")]),
    doc("i2", "ACCREC", "AUTHORISED", "2026-03-20", "Promoter", [line("Deposit", "200", "2000.00")],
        reference="260328ABB deposit"),
    doc("b4", "ACCPAY", "DRAFT", "2026-03-21", "Sound Co", [line("Draft ABBA", "310", "50.00")]),
    doc("b3", "ACCPAY", "AUTHORISED", "2026-03-05", "Ergon", [line("Electricity", "445", "640.00")]),
    doc("i3", "ACCREC", "AUTHORISED", "2026-03-06", "Promoter", [line("Bond", "610", "100.00")]),
]
BANK = [
    {"BankTransactionID": "bt1", "Type": "SPEND", "Status": "AUTHORISED", "DateString": "2026-04-02T00:00:00",
     "BankAccount": {"AccountID": "a-bank"}, "Contact": {"ContactID": "c-cater", "Name": "Catering Co"},
     "Reference": "Lunch", "Total": "450.00", "LineItems": [line("Catering", "310", "450.00")]},
    {"BankTransactionID": "bt2", "Type": "SPEND-TRANSFER", "Status": "AUTHORISED",
     "DateString": "2026-03-10T00:00:00", "BankAccount": {"AccountID": "a-bank"}, "LineItems": []},
]
JOURNALS = [
    {"ManualJournalID": "mj1", "Status": "POSTED", "DateString": "2026-03-31T00:00:00",
     "Narration": "Accrue ABBA artist fee", "JournalLines": [
         {"Description": "Artist fee", "AccountCode": "310", "LineAmount": "1000.00"},
         {"Description": "Accrual", "AccountCode": "800", "LineAmount": "-1000.00"}]},
]


@pytest.fixture
def org(db):
    user = get_user_model().objects.create_user("finance", password="pw")
    org = Organisation.objects.create(name="TECC")
    org.members.add(user)
    return org, user


@pytest.fixture
def client(client, org):
    client.force_login(org[1])
    return client


@pytest.fixture
def data():
    return {"invoices": copy.deepcopy(INVOICES), "bank": copy.deepcopy(BANK), "journals": copy.deepcopy(JOURNALS)}


@pytest.fixture(autouse=True)
def source(monkeypatch, data):
    source = fake_source()
    source.raw_invoices = lambda start, end: [r for r in data["invoices"] if start.isoformat() <= r["DateString"][:10]
                                              <= end.isoformat()]
    source.raw_credit_notes = lambda start, end: []
    source.raw_bank_transactions = lambda start, end: data["bank"]
    source.raw_manual_journals = lambda start, end: data["journals"]
    monkeypatch.setattr("console.views.source_for", lambda organisation: source)
    return source


@pytest.fixture
def event(org):
    org = org[0]
    category = TrackingCategory.objects.create(organisation=org, name="Event", source_system="xero",
                                               source_id="tc-event")
    option = TrackingOption.objects.create(organisation=org, category=category, name="Celtic Nights",
                                           source_system="xero", source_id="to-cn")
    dim = Dimension.objects.create(
        organisation=org, code="event", name="Event", tracking_category=category, replaces_source=True,
        source_name="Event", attribute_fields=[{"key": "start_date", "label": "Start date", "type": "date"},
                                               {"key": "end_date", "label": "End date", "type": "date"}])
    for code, name, first, last, opt in [
        ("Celtic Nights", "Celtic Nights 2026", "2026-03-14", "2026-03-14", option),
        ("260328ABB", "ABBA", "2026-03-28", "2026-03-28", None),
        ("260331ADF", "ADF Transition Seminar", "2026-03-31", "2026-03-31", None),
        ("XMAS", "Christmas Party", None, None, None),
    ]:
        attrs = {"start_date": first, "end_date": last} if first else {}
        DimensionValue.objects.create(organisation=org, dimension=dim, code=code, name=name, tracking_option=opt,
                                      attributes=attrs)
    return dim


def say(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert data["ok"], data
    return data["result"]


def error(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert not data["ok"]
    return data["error"]


def notes(result):
    return " ".join(b["text"] for b in result["blocks"] if b["type"] == "note")


def rows(result):
    table = next(b for b in result["blocks"] if b["type"] == "table" and b["title"].startswith("Transactions"))
    return [r["cells"] for r in table["rows"]]


def by_number(result):
    return {r[3]: r for r in rows(result)}


# --- Copying from Xero ------------------------------------------------------------------------------------------

def test_copy_period_copies_posted_transactions_and_skips_the_rest(org, source, data, event):
    from datetime import date

    result = copy_period(org[0], source, ["invoices", "bills", "bank", "journals"], date(2026, 3, 1),
                         date(2026, 4, 30))
    assert (result.added, result.updated, result.unchanged) == (8, 0, 0)  # 6 posted docs, 1 bank, 1 journal
    assert not Document.objects.filter(source_id="b4").exists()  # draft
    assert BankTransaction.objects.count() == 1  # the transfer is skipped
    b1 = Document.objects.get(source_id="b1")
    assert b1.type == "bill" and b1.has_attachments and b1.contact.name == "Sound Co"
    assert b1.lines.get().account.code == "310"
    journal = Journal.objects.get()
    assert [str(li.amount) for li in journal.lines.all()] == ["1000.00", "-1000.00"]
    # Xero tracking becomes a tag in the linked iLA dimension
    i1_line = Document.objects.get(source_id="i1").lines.get()
    assert i1_line.tags.get(dimension=event).dimension_value.code == "Celtic Nights"

    again = copy_period(org[0], source, ["invoices", "bills", "bank", "journals"], date(2026, 3, 1),
                        date(2026, 4, 30))
    assert (again.added, again.updated, again.unchanged) == (0, 0, 8)
    data["invoices"][0]["Status"] = "VOIDED"
    data["invoices"][0]["LineItems"][0]["Description"] = "Changed"
    third = copy_period(org[0], source, ["bills"], date(2026, 3, 1), date(2026, 4, 30))
    assert third.updated == 1
    assert Document.objects.get(source_id="b1").status == "voided"
    assert Document.objects.get(source_id="b1").lines.get().description == "Changed"


# --- /tag -------------------------------------------------------------------------------------------------------

def test_tag_proposes_from_evidence(client, event):
    result = say(client, "/tag --dim event --all --Mar 2026 to Apr 2026")
    assert result["title"].startswith("/tag · Event · 01 Mar 2026 to 30 Apr 2026")
    found = by_number(result)
    assert set(found) == {"I1", "B1", "B2", "I2", "B3", "Lunch", ""}  # i3 has no P&L line
    # i1 is tagged through Xero's tracking: listed with both columns filled, but not proposed again
    assert found["I1"][7:12] == ["Celtic Nights", "Celtic Nights", "", "",
                                 "Already tagged; set it to change, or add --retag for a proposal"]
    assert found["B1"][7:12] == ["", "", "260328ABB ABBA", "High", '"ABBA" in the text, and the date fits']
    assert found["I2"][9:12] == ["260328ABB ABBA", "High", "Code 260328ABB in the text"]
    assert found[""][9:11] == ["260328ABB ABBA", "High"]  # the journal's narration names ABBA
    assert found[""][6] == "1,000.00"  # a journal's P&L amount is its debits
    assert found["Lunch"][9:12] == ["", "", "Only the date fits 260331ADF; the date alone isn't enough"]
    assert found["B2"][9:12] == ["", "", "Only the date fits 260328ABB, 260331ADF; the date alone isn't enough"]
    assert found["B3"][11] == "No evidence"
    figures = next(b for b in result["blocks"] if b["type"] == "figures")["items"]
    assert figures[2]["value"] == "3 high · 0 medium · 0 low"
    assert (figures[3]["value"], figures[4]["value"]) == ("3", "1")
    assert not Assignment.objects.filter(method=Tag.Method.MANUAL).exists()  # nothing tagged before approval


def test_approve_by_confidence_tags_whole_transactions(client, org, event):
    say(client, "/tag --dim event --all --Mar 2026 to Apr 2026")
    result = say(client, "approve high")
    assert result["title"] == "Tagged 3 transaction(s) to Event"
    assert "3 row(s) weren't approved" in notes(result)
    b1 = Document.objects.get(source_id="b1")
    assignment = Assignment.objects.get(document=b1)
    assert (assignment.level, assignment.method, assignment.status) == ("transaction", "manual", "confirmed")
    assert assignment.set_by == org[1]
    assert b1.lines.get().tags.get().dimension_value.code == "260328ABB"
    assert TagRun.objects.get().status == TagRun.Status.APPROVED

    again = by_number(say(client, "/tag --dim event --all --Mar 2026 to Apr 2026"))
    assert again["B1"][8:10] == ["260328ABB", ""]  # now tagged in iLA, so no new proposal
    assert not [n for n, r in again.items() if r[9]]  # nothing left with a proposal


def test_skip_set_and_approve_rows(client, event):
    result = say(client, "/tag --dim event --bills --Mar 2026")
    numbers = [r[3] for r in rows(result)]
    assert numbers == ["B3", "B1", "B2"]
    say(client, "skip 2")
    result = say(client, "set 1,3 XMAS")
    assert "Row(s) 1, 3 set to XMAS Christmas Party" in notes(result)
    assert "have no value yet" in notes(say(client, "approve 2"))
    result = say(client, "approve")
    assert result["title"] == "Tagged 2 transaction(s) to Event"
    assert {a.document.source_id for a in Assignment.objects.filter(method="manual")} == {"b3", "b2"}


def test_to_with_filters(client, event):
    result = say(client, "/tag --dim event --bills --invoices --Mar 2026 --desc abba or electricity "
                         "--amount >500 --to ABBA")
    assert [r[3] for r in rows(result)] == ["B3", "B1"]
    assert {r[9] for r in rows(result)} == {"260328ABB ABBA"}
    assert [r[3] for r in rows(say(client, "/tag --dim event --bills --Mar 2026 --contact sound"))] == ["B1"]
    assert [r[3] for r in rows(say(client, "/tag --dim event --bills --Mar 2026 --attachment"))] == ["B1"]
    assert [r[3] for r in rows(say(client, "/tag --dim event --bills --Mar 2026 --account 412"))] == ["B2"]
    assert [r[3] for r in rows(say(client, "/tag --dim event --bills --Mar 2026 --amount 300 to 700"))] == \
        ["B3", "B2"]
    result = say(client, "/tag --dim event --invoices --Mar 2026 --xero Event Celtic Nights --retag")
    assert [r[3] for r in rows(result)] == ["I1"]
    assert rows(result)[0][7:12] == ["Celtic Nights", "Celtic Nights", "Celtic Nights Celtic Nights 2026", "High",
                                     "Xero tracking Celtic Nights"]
    # without --retag the tracked invoice is listed, but --to doesn't override it
    result = say(client, "/tag --dim event --invoices --Mar 2026 --xero Event --to ABBA")
    assert rows(result)[0][9] == "" and "Already tagged" in rows(result)[0][11]


def test_mistakes_are_explained(client, event):
    assert "Say what to tag" in error(client, "/tag --dim event --Mar 2026")
    assert "Give the period" in error(client, "/tag --dim event --bills")
    assert 'No iLA dimension called "venue"' in error(client, "/tag --dim venue --bills --Mar 2026")
    assert 'No Event value matches "nope"' in error(client, "/tag --bills --Mar 2026 --to nope")
    assert "--xero needs one of Xero's tracking categories (Event, Region)" in error(
        client, "/tag --bills --Mar 2026 --xero Venue")
    assert "isn't an amount" in error(client, "/tag --bills --Mar 2026 --amount lots")
    assert 'Put every option after "--"' in error(client, "/tag abba --bills --Mar 2026")


def test_cancel_drops_the_preview(client, event):
    say(client, "/tag --bills --Mar 2026")
    assert say(client, "/cancel")["title"] == "Tagging cancelled"
    assert TagRun.objects.get().status == TagRun.Status.CANCELLED
    assert not Assignment.objects.filter(method="manual").exists()


def test_a_distinctive_word_beats_the_same_supplier(client, org, data, event):
    from decimal import Decimal

    from core import tagging

    for code, first in [("260826MEC", "2026-08-26"), ("260902MEC", "2026-09-02"), ("260910COW", "2026-09-12")]:
        name = "Cowboys Presentation Night" if code.endswith("COW") else "MECCA Training"
        DimensionValue.objects.create(organisation=org[0], dimension=event, code=code, name=name,
                                      attributes={"start_date": first, "end_date": first})
    data["invoices"] += [
        doc("w3844", "ACCPAY", "PAID", "2026-09-18", "Wild Gravity", [line("End of season AV", "310", "9283.00")]),
        doc("w3825", "ACCPAY", "PAID", "2026-09-09", "Wild Gravity", [line("Mecca", "310", "3312.00")]),
        doc("w3840", "ACCPAY", "PAID", "2026-09-14", "Wild Gravity", [line("SMMC Graduation", "310", "16287.00")]),
        doc("t1", "ACCPAY", "PAID", "2026-09-15", "Trainer", [line("Staff training", "310", "100.00")]),
    ]
    say(client, "/tag --dim event --bills --Sep 2026 --contact wild")  # copies the bills
    cow = DimensionValue.objects.get(code="260910COW")
    tagging.assign(Document.objects.get(source_id="w3844"), event, [(cow, Decimal("1"))], method="manual",
                   confidence="high", status="confirmed")

    found = by_number(say(client, "/tag --dim event --bills --Sep 2026"))
    assert found["W3825"][9:12] == ["260902MEC MECCA Training", "Medium",
                                    '"mecca" (from MECCA Training) in the text; the nearest MECCA Training (02 Sep)']
    assert found["W3840"][9:11] == ["260910COW Cowboys Presentation Night", "Low"]  # only the supplier and date
    # "training" alone doesn't point at MECCA Training, and the date alone proposes nothing
    assert found["T1"][9:12] == ["", "", "Only the date fits 260910COW; the date alone isn't enough"]


def test_pl_by_event_combines_xero_tracking_and_ila_tags_and_ties_to_xero(client, org, source, event):
    from decimal import Decimal

    from core import tagging
    from xero.source import ProfitAndLoss

    D = Decimal
    source.profit_and_loss = lambda start, end: ProfitAndLoss(
        by_account={"a-sales": D("7000"), "a-cogs": D("2250"), "a-cons": D("300"), "a-power": D("640"),
                    "a-wages": D("1234")},
        category_of={"a-sales": "revenue", "a-cogs": "cost_of_sales", "a-cons": "expenses",
                     "a-power": "expenses", "a-wages": "expenses"},
        net_profit=D("2576"))
    say(client, "/tag --dim event --all --Mar 2026 to Apr 2026")
    say(client, "approve high")  # b1, i2 and the journal to ABBA; i1 is on Celtic Nights through Xero tracking
    abba, adf = DimensionValue.objects.get(code="260328ABB"), DimensionValue.objects.get(code="260331ADF")
    tagging.assign(Document.objects.get(source_id="b2"), event, [(abba, D("0.5")), (adf, D("0.5"))],
                   method="manual", confidence="high", status="confirmed")

    result = say(client, "/pl --event --Mar 2026 to Apr 2026")
    table = next(b for b in result["blocks"] if b["type"] == "table")
    assert [c["label"] for c in table["columns"]] == [
        "Code", "Account", "Celtic Nights Celtic Nights 2026", "260328ABB ABBA", "260331ADF ADF Transition Seminar",
        "Unassigned", "Total in iLA", "Xero P&L", "Difference"]
    rows = {r["cells"][1]: r["cells"][2:] for r in table["rows"]}
    assert rows["Sales"] == ["5,000.00", "2,000.00", "0.00", "0.00", "7,000.00", "7,000.00", "0.00"]
    assert rows["Cost of Goods Sold"] == ["0.00", "1,800.00", "0.00", "450.00", "2,250.00", "2,250.00", "0.00"]
    assert rows["Consulting"] == ["0.00", "150.00", "150.00", "0.00", "300.00", "300.00", "0.00"]  # split 50/50
    assert rows["Wages"] == ["0.00", "0.00", "0.00", "0.00", "0.00", "1,234.00", "1,234.00"]  # payroll: not read
    assert rows["Net profit"][-3:] == ["3,810.00", "2,576.00", "(1,234.00)"]
    assert "Largest: 477 Wages 1,234.00" in notes(result)

    assert say(client, "/pl --by Event --Mar 2026")["title"] == "Profit and loss by Event: March 2026"
    assert "Use two dashes: /pl --event" in error(client, "/pl -event")
    assert 'No iLA dimension called "venue"' in error(client, "/pl --by venue")
