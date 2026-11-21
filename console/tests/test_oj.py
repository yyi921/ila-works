"""/OJ: composing overlay journals in the console, the live panel, the import file that makes a draft To be posted,
private drafts, and leaving the bucket once in Xero."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from console.models import OJSession
from core import overlay
from core.models import (
    Account,
    Adjustment,
    AuditLog,
    Dimension,
    DimensionValue,
    ImportFile,
    Journal,
    Organisation,
    TrackingCategory,
    TrackingOption,
)

from .fake_xero import fake_source

D = Decimal


@pytest.fixture
def org(db):
    user = get_user_model().objects.create_user("finance", password="pw")
    org = Organisation.objects.create(name="TECC")
    org.members.add(user)
    for code, name, kind in [("200", "Sales", "revenue"), ("477", "Wages", "expense"), ("469", "Rent", "expense"),
                             ("800", "Accounts Payable", "liability"), ("090", "Business Bank", "asset")]:
        Account.objects.create(organisation=org, code=code, name=name, account_class=kind)
    departments = TrackingCategory.objects.create(organisation=org, name="Departments", source_system="xero",
                                                  source_id="tc-d")
    TrackingOption.objects.create(organisation=org, category=departments, name="Food and Beverage Services",
                                  source_system="xero", source_id="to-fb")
    TrackingOption.objects.create(organisation=org, category=departments, name="Overhead & Venue Costs",
                                  source_system="xero", source_id="to-ov")
    events = TrackingCategory.objects.create(organisation=org, name="Event", source_system="xero", source_id="tc-e")
    dim = Dimension.objects.create(organisation=org, code="event", name="Event", tracking_category=events)
    for code, start in (("260930DYN", date(2026, 9, 30)), ("250930DYN", date(2025, 9, 30))):
        option = TrackingOption.objects.create(organisation=org, category=events, name=code, source_system="xero",
                                               source_id=f"to-{code}")
        DimensionValue.objects.create(organisation=org, dimension=dim, code=code, name="Dynamic lunch",
                                      tracking_option=option, start_date=start)
    return org, user


@pytest.fixture
def client(client, org):
    client.force_login(org[1])
    return client


@pytest.fixture(autouse=True)
def source(monkeypatch):
    source = fake_source()
    for name in ("raw_invoices", "raw_credit_notes", "raw_bank_transactions", "raw_manual_journals"):
        setattr(source, name, lambda start, end: [])
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


def money(text):
    text = text.replace(",", "")
    return -D(text.strip("()")) if text.startswith("(") else D(text or "0")


def headline(result):
    return {f["label"]: money(f["value"]) for f in result["panel"]["headline"]}


def rows(result, side):
    table = next(b for b in result["panel"][side]["blocks"] if b["type"] == "table")
    return [r["cells"] for r in table["rows"]]


def test_compose_tweak_listen(client, org):
    opened = say(client, "/OJ --Sep 2026")
    assert opened["panel"]["title"] == "OJ · September 2026 · TECC"
    xero = headline(opened)["Net profit · Xero"]
    assert headline(opened)["After"] == xero and "No overlay journals" in str(opened["panel"]["left"])

    assert "OJ-0001-d started, dated 30/09/2026" in say(client, "new Wages accrual")["panel"]["message"]
    say(client, "dr 477 1200 fb DYN stub wages")
    result = say(client, "cr 800")
    assert rows(result, "left") == [
        ["1", "477 Wages", "Food and Beverage Services · 260930DYN", "1,200.00", "", "stub wages"],
        ["2", "800 Accounts Payable", "", "", "1,200.00", ""], ["", "Total", "", "1,200.00", "1,200.00", ""]]
    assert headline(result) == {"Net profit · Xero": xero, "To be posted": D("0"), "Draft": D("-1200"),
                                "After": xero - 1200}
    pl = {r[0]: r for r in rows(result, "right")}
    assert pl["Labour"][2:4] == ["", "1,200.00"] and money(pl["Net profit"][3]) == D("-1200")

    result = say(client, "1 1500")
    assert "Out by 300.00: credit needed." in str(result["panel"]["left"])
    assert say(client, "undo")["panel"]["message"].startswith("Undone: Line 1")
    assert headline(say(client, "redo"))["Draft"] == D("-1500")
    say(client, "undo")
    say(client, "469 100 to 800 ov")
    result = say(client, "move 50 from 477 to 469")
    assert [r[1] for r in rows(result, "left")][2:6] == ["469 Rent", "800 Accounts Payable", "469 Rent", "477 Wages"]
    say(client, "del 6")
    result = say(client, "del 5")
    assert len(rows(result, "left")) == 5 and headline(result)["Draft"] == D("-1300")
    assert rows(say(client, "3 event DYN"), "left")[2][2] == "Overhead & Venue Costs · 260930DYN"
    assert rows(say(client, "3 dept Food and Beverage Services"), "left")[2][2].startswith("Food and Beverage")
    assert rows(say(client, "3 dept ov"), "left")[2][2] == "Overhead & Venue Costs · 260930DYN"
    assert "several accounts" not in error(client, "dr zzz 5") and "No account" in error(client, "dr zzz 5")

    labour = rows(say(client, "pl lab"), "right")
    assert labour[0][:2] == ["477", "Wages"] and labour[0][4] == "1,200.00"
    bs = {r[0]: r for r in rows(say(client, "bs"), "right")}
    assert bs["800"][4] == "1,300.00"  # a liability: the credit shows as an increase
    why = rows(say(client, "why 477"), "right")
    assert why[0][1] == "OJ-0001-d" and why[-1][3] == "1,200.00"

    assert "Reverses on 01/10/2026" in say(client, "rev")["panel"]["message"]
    adj = Adjustment.objects.get()
    assert overlay.effect([adj], date(2026, 10, 1), date(2026, 10, 31))["477"]["draft"] == D("-1200")
    assert say(client, "rev 15/10")["panel"]["message"] == "Reverses on 15/10/2026."
    say(client, "rev off")
    assert "versions" in say(client, "versions")["panel"]["message"]

    closed = say(client, "done")
    assert closed["panel"] == {"closed": True} and not OJSession.objects.filter(status="open").exists()
    assert Adjustment.objects.get().reverse_date is None  # everything was saved as typed


def test_generating_the_file_makes_it_to_be_posted(client, org):
    say(client, "/OJ --Sep 2026")
    say(client, "new Wages accrual")
    say(client, "dr 477 1200")
    assert "doesn't balance" in error(client, "file")
    say(client, "cr 800")
    say(client, "rev")
    other = get_user_model().objects.create_user("other", password="pw")
    org[0].members.add(other)
    assert overlay.visible(org[0], other) == []  # a draft is private

    result = say(client, "file")
    assert "OJ-0001-p1 is now To be posted" in result["panel"]["message"]
    adj = Adjustment.objects.get()
    assert (adj.status, adj.reference, adj.import_file.status) == ("to_post", "OJ-0001-p1", "stamped")
    assert [a.pk for a in overlay.visible(org[0], other)] == [adj.pk]
    body = client.get(result["link"]).content.decode()
    lines = list(csv.reader(io.StringIO(body)))
    assert lines[0] == overlay.COLUMNS and len(lines) == 5  # the journal and its reversal
    assert lines[1] == ["Wages accrual [OJ-0001-p1]", "30/09/2026", "", "477", "BAS Excluded", "1200.00", "", "", "",
                        ""]
    assert lines[3][:2] == ["Reversal: Wages accrual [OJ-0001-p1]", "01/10/2026"] and lines[3][5] == "-1200.00"
    assert headline(result)["To be posted"] == D("-1200") and headline(result)["Draft"] == D("0")
    assert "locked" in error(client, "1 1500")

    result = say(client, "list")
    table = next(b for b in result["panel"]["left"]["blocks"] if b["type"] == "table")
    assert [a["command"] for a in table["rows"][0]["actions"]] == ["open 1", "back 1"]
    assert "is a draft again" in say(client, "back 1")["panel"]["message"]
    adj.refresh_from_db()
    assert adj.reference == "OJ-0001-d" and ImportFile.objects.get().status == "rejected"
    say(client, "1 1500")
    say(client, "2 1500")
    assert "OJ-0001-p2" in say(client, "file 1")["panel"]["message"]

    # An older file imported by mistake is flagged; the current one makes the journal leave the bucket.
    Journal.objects.create(organisation=org[0], date=date(2026, 9, 30), status="posted", source_system="xero",
                           source_id="j1", narration="Wages accrual [OJ-0001-p1]")
    assert "from an earlier file" in say(client, "refresh")["panel"]["message"]
    Journal.objects.filter(source_id="j1").update(narration="Wages accrual [OJ-0001-p2]")
    result = say(client, "refresh")
    assert "Now in Xero, so out of the bucket: OJ-0001-p2" in result["panel"]["message"]
    assert not Adjustment.objects.exists() and headline(result)["To be posted"] == D("0")
    assert AuditLog.objects.filter(input__startswith="OJ-0001-p2 left the bucket: found in Xero").exists()
    assert "OJ-0002-d" in say(client, "new Next one")["panel"]["message"]  # 0001 is never used again
    assert "OJ-0002-d deleted" in say(client, "drop")["panel"]["message"]
    assert "OJ-0003-d" in say(client, "new And another")["panel"]["message"]


def test_ilas_draft_becomes_your_copy(client, org):
    ila = Adjustment.objects.create(organisation=org[0], date=date(2026, 9, 30), period_end=date(2026, 9, 30),
                                    narration="Payroll FNE", origin="labour", origin_ref="7")
    ila.lines.create(line_number=1, account_code="477", amount=D("500"))
    ila.lines.create(line_number=2, account_code="800", amount=D("-500"))
    say(client, "/OJ --Sep 2026")
    assert "iLA's" in say(client, "open 1")["panel"]["message"]
    result = say(client, "1 600")
    assert "your own copy, OJ-0002" in result["panel"]["message"]
    assert [a.code for a in overlay.visible(org[0], org[1])] == ["OJ-0002"]  # yours in place of iLA's, for you
    assert Adjustment.objects.count() == 2 and headline(result)["Draft"] == D("-600")
    say(client, "2 600")
    say(client, "file")
    assert [a.reference for a in Adjustment.objects.all()] == ["OJ-0002-p1"]  # iLA's has left the bucket
    assert "is iLA's" not in error(client, "dr 477") and "locked" in error(client, "dr 477 5")


def test_console_page_has_the_panel_and_resumes(client, org):
    page = client.get("/console/").content.decode()
    assert 'id="panel"' in page and '<script id="resume" type="application/json">""</script>' in page
    say(client, "/OJ --Sep 2026")
    assert '<script id="resume" type="application/json">"oj"</script>' in client.get("/console/").content.decode()
    assert say(client, "/OJ")["panel"]["title"].startswith("OJ · September 2026")


def test_pl_shows_the_journals_on_top_while_composing(client, org):
    plain = say(client, "/pl --Sep 2026")
    assert [c["label"] for c in plain["blocks"][0]["columns"]][2:4] == ["Actual", "Budget"]  # as before
    say(client, "/OJ --Sep 2026")
    say(client, "new Wages accrual")
    say(client, "dr 477 1200")
    say(client, "cr 800")

    result = say(client, "/pl --vs ly")  # no month needed: the one being composed, with the journals on top
    assert result["title"] == "Profit and loss: September 2026 vs last year" and "panel" not in result
    table = result["blocks"][0]
    assert [c["label"] for c in table["columns"]] == ["Code", "Account", "Xero", "To be posted", "Draft", "After",
                                                       "Last year", "Var $", "Var %"]
    rows = {r["cells"][1]: r for r in table["rows"]}
    wages = rows["Wages"]["cells"]
    assert wages[4] == "1,200.00" and money(wages[5]) == money(wages[2]) + 1200
    assert money(wages[7]) == money(wages[5]) - money(wages[6]) and rows["Wages"]["tone"] == "touch"
    net = rows["Net profit"]["cells"]
    assert money(net[4]) == D("-1200") and money(net[5]) == money(net[2]) - 1200

    xero_only = say(client, "/pl --vs ly --xero")["blocks"][0]
    assert [c["label"] for c in xero_only["columns"]][2:4] == ["Actual", "Last year"]
    assert "Xero" in [c["label"] for c in say(client, "/pl --Sep 2026 --landing")["blocks"][0]["columns"]]
    assert say(client, "list")["panel"]["left"]["title"] == "The bucket"  # the panel is still there


def test_plain_english_becomes_commands_and_is_remembered(client, org, monkeypatch):
    from console import llm, oj_plain
    from console.models import OJMemory, OJSettings
    from console.periods import month_period

    Account.objects.create(organisation=org[0], code="2000", name="Accruals", account_class="liability")
    Account.objects.create(organisation=org[0], code="6820", name="Audit Fees", account_class="expense")
    asked, answers = [], []
    monkeypatch.setattr(llm, "available", lambda: True)

    def fake(*, system, content, tool_name, description, schema):
        asked.append(content)
        return answers.pop(0)

    monkeypatch.setattr(llm, "call_tool", fake)
    say(client, "/OJ --Sep 2026")

    answers.append({"steps": ["new Audit fee accrual", "dr 6820 1200 Overhead & Venue Costs | Audit fee accrual",
                              "cr 2000 | Audit fee accrual"], "question": "", "used": "chart"})
    result = say(client, "accrue audit fee for another 1200")
    assert "From your words: new Audit fee accrual" in result["panel"]["message"]
    assert "chosen from the chart: check them" in result["panel"]["message"]
    assert rows(result, "left")[:2] == [
        ["1", "6820 Audit Fees", "Overhead & Venue Costs", "1,200.00", "", "Audit fee accrual"],
        ["2", "2000 Accruals", "", "", "1,200.00", "Audit fee accrual"]]
    assert OJSettings.objects.get().accrual_account == "2000"  # guessed from the chart
    context = asked[0]
    assert "Accrual account (credited by an accrual): 2000" in context and "6820\tAudit Fees\texpense" in context
    assert "Open journal: none." in context and context.endswith("Request: accrue audit fee for another 1200")

    answers.append({"steps": ["1 acc 469"], "question": "", "used": "chart"})
    result = say(client, "change the GL 6820 to 469")
    assert rows(result, "left")[0][1] == "469 Rent" and 'line 1: dr 6820 1200.00' in asked[1]
    assert say(client, "undo")["panel"]["message"].startswith("Undone")  # each step can be undone

    # All or nothing; the model can't finish a journal; a question is passed on.
    answers.append({"steps": ["1 1500", "dr 9999 5"], "question": "", "used": "chart"})
    assert "Nothing was changed" in error(client, "bump it and add a mystery line")
    assert Adjustment.objects.get().lines.get(line_number=1).amount == D("1200")
    answers.append({"steps": ["file"], "question": "", "used": "chart"})
    assert "isn't allowed" in error(client, "generate the file for me")
    answers.append({"steps": [], "question": "Which fee: audit or legal?", "used": ""})
    assert say(client, "accrue the fee again")["panel"]["message"] == "Which fee: audit or legal?"

    # Generating the file remembers the journal, with the words used; next time it's offered to the model first.
    say(client, "file")
    memory = OJMemory.objects.get()
    assert (memory.key, memory.asked) == ("audit fee accrual", "accrue audit fee for another 1200 / change the GL "
                                                               "6820 to 469")
    assert memory.lines[0] == {"side": "dr", "account": "6820", "amount": "1200.00",
                               "tracking": ["Overhead & Venue Costs"], "tax": ""}
    assert 'Memory (journals that were right before' in oj_plain.context(
        org[0], "accrue audit fee", month_period(2026, 10), None)


def test_plain_english_without_the_model_says_so(client, org, monkeypatch):
    from console import llm

    monkeypatch.setattr(llm, "available", lambda: False)
    say(client, "/OJ --Sep 2026")
    assert "ANTHROPIC_API_KEY isn't set" in error(client, "accrue audit fee for another 1200")
