from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounting import jobs
from accounting.models import Decision, Expectation
from accounting.scan import scan
from accounting.tests.test_engine import END, START, tecc  # noqa: F401  (the TECC-like ledger)
from core.models import Document, ImportFile

from .fake_xero import fake_source

D = Decimal


@pytest.fixture
def client(client, tecc):  # noqa: F811
    user = get_user_model().objects.create_user("finance", password="pw")
    tecc.org.members.add(user)
    client.force_login(user)
    scan(tecc.accrual, START, END)
    return client


@pytest.fixture(autouse=True)
def jobs_now(monkeypatch):
    monkeypatch.setattr("accounting.jobs.start", lambda work: work())
    monkeypatch.setattr("console.views.source_for", lambda organisation: fake_source())


def say(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert data["ok"], data
    return data["result"]


def error(client, text):
    data = client.post("/console/run/", {"command": text}).json()
    assert not data["ok"], data
    return data["error"]


def tables(result):
    return {b["title"]: b for b in result["blocks"] if b["type"] == "table"}


def notes(result):
    return " ".join(b["text"] for b in result["blocks"] if b["type"] == "note")


def figures(result):
    return {f["label"]: f["value"] for b in result["blocks"] if b["type"] == "figures" for f in b["items"]}


def test_review_by_source_and_decide(client, tecc):  # noqa: F811
    result = say(client, "/accrue --Sep 2026")
    assert result["title"] == "Accruals at 30 Sep 2026"
    bills = tables(result)["Bills after month end"]["rows"]
    assert bills[0]["cells"][:4] == ["1", "6100 Electricity", "CS Energy", "26,000.00"]
    assert "bill T1100011 (CS Energy, 06 Oct)" in bills[0]["cells"][4]
    assert "payroll, worked out separately" in notes(result)

    result = say(client, "accept 1")
    assert tables(result)["Bills after month end"]["rows"][0]["cells"][5] == "Accepted"
    assert figures(result)["Accepted"] == "26,000.00"
    say(client, "adjust 1 25,500 Credit note expected for the outage")
    line = Expectation.objects.get(account=tecc.power)
    assert (line.decision, line.expected_amount, line.proposed_amount) == (Decision.ADJUSTED, D("25500"), D("26000"))
    assert "then the reason" in error(client, "reject 1")  # a reason is required
    say(client, "reject 1 Billed in October after all")
    assert Expectation.objects.get(account=tecc.power).decision == Decision.REJECTED
    say(client, "undo 1")
    assert Expectation.objects.get(account=tecc.power).expected_amount == D("26000")


def test_why_shows_the_evidence_and_what_was_learned(client):
    say(client, "/accrue --Sep 2026")
    result = say(client, "why 1")
    evidence = tables(result)["Evidence"]["rows"][0]["cells"]
    assert evidence[:2] == ["T1100011", "CS Energy"] and evidence[4] == "billing lag"
    assert "What iLA learned: Accrued to 6100 Electricity in 12 of the 12 months" in notes(result)


def test_build_only_with_accepted_lines(client):
    say(client, "/accrue --Sep 2026")
    assert "No line is accepted yet" in error(client, "build")
    say(client, "accept bills")
    result = say(client, "build")
    assert result["title"].startswith("Journal file built") and result["link"].startswith("/console/files/")
    assert ImportFile.objects.get().status == ImportFile.Status.STAMPED


def test_compare_and_refresh_keep_decisions(client, tecc):  # noqa: F811
    say(client, "/accrue --Sep 2026")
    say(client, "accept 1")
    result = say(client, "refresh")
    assert "decided lines were kept" in notes(result)
    assert Expectation.objects.get(account=tecc.power).decision == Decision.ACCEPTED
    result = say(client, "compare")
    rows = {r["cells"][0]: r["cells"] for r in tables(result)[""]["rows"]}
    assert rows["6100 Electricity"][1] == "26,000.00" and rows["6100 Electricity"][2] == "26,000.00"


def test_read_starts_the_pdf_job(client, tecc, monkeypatch):  # noqa: F811
    Document.objects.filter(number="T1100011").update(source_id="x-bill", has_attachments=True)
    started = []
    monkeypatch.setattr("accounting.jobs.read_bills", lambda accrual, period_end, user=None: started.append(period_end)
                        or True)
    monkeypatch.setattr("console.accrue.jobs.read_bills", jobs.read_bills)
    result = say(client, "/accrue --read --Sep 2026")
    assert result["title"] == "Reading 1 bill PDF(s) in the background" and started == [date(2026, 9, 30)]


def test_no_set_up_and_month_not_ended(client, tecc):  # noqa: F811
    assert "hasn't ended yet" in error(client, "/accrue --Dec 2099")
    tecc.accrual.profiles.all().delete()
    assert "no accrual set-up yet" in error(client, "/accrue --Sep 2026")
