"""Reading bill PDFs to place a bill in its month, and the effect on a run."""

from datetime import date
from decimal import Decimal

import pytest

from accounting import jobs
from accounting.ledger import run_accruals
from accounting.models import Accrual, BillReading, State
from accounting.reading import placement, read_bill
from accounting.scan import scan
from core.models import Document

from .test_engine import END, START, tecc  # noqa: F401  (the TECC-like ledger)

D = Decimal
SEP = date(2026, 9, 30)


class FakeSource:
    """Attachments of bills by Xero id: {source id: [(name, type, bytes)]}."""

    def __init__(self, files):
        self.files, self.downloads = files, 0

    def bill_attachments(self, invoice_id):
        return [{"id": f"{invoice_id}-{n}", "name": name, "type": kind, "size": len(content)}
                for n, (name, kind, content) in enumerate(self.files.get(invoice_id, []))]

    def bill_attachment(self, invoice_id, attachment_id):
        self.downloads += 1
        name, kind, content = self.files[invoice_id][int(attachment_id.rsplit("-", 1)[1])]
        return content, kind


@pytest.fixture
def model(monkeypatch):
    """The language model: returns the facts set for each bill number."""
    answers, calls = {}, []

    def call_tool(**kwargs):
        text = kwargs["content"][1]["text"]
        calls.append(text)
        number = text.split()[1]
        if isinstance(answers.get(number), Exception):
            raise answers[number]
        return answers.get(number, {"events": [], "lines": []})

    monkeypatch.setattr("console.llm.call_tool", call_tool)
    monkeypatch.setattr("console.llm.available", lambda: True)
    return answers, calls


# --- Placement rules ---------------------------------------------------------------------------------------------


def test_events_decide_the_month():
    held = {"events": [{"name": "BRA conference", "date": "2026-09-24", "quote": "Event: 24/09/2026", "page": 1}]}
    place = placement(held, SEP)
    assert place.kind == "month" and "event BRA conference on 24 Sep 2026" in place.reason
    assert place.quote == "Event: 24/09/2026" and place.page == 1
    later = {"events": [{"name": "MEL", "date": "2026-10-08"}]}
    assert placement(later, SEP).kind == "later"


def test_dated_lines_split_a_bill_across_month_end():
    facts = {"events": [], "lines": [{"description": "Crew 29 Sep", "date": "2026-09-29", "amount": 750},
                                     {"description": "Crew 2 Oct", "date": "2026-10-02", "amount": 250}]}
    place = placement(facts, SEP)
    assert place.kind == "spans" and place.share == D("0.75")


def test_service_period_by_days_and_nothing_to_go_on():
    usage = {"events": [], "lines": [], "service_period": {"from": "2026-09-16", "to": "2026-10-15",
                                                           "quote": "Usage 16/09 - 15/10"}}
    place = placement(usage, SEP)
    assert place.kind == "spans" and place.share == D("0.5")  # 15 of 30 days
    assert placement({"service_period": {"to": "2026-09-30"}, "events": [], "lines": []}, SEP).kind == "month"
    assert placement({"events": [], "lines": []}, SEP).kind == "unknown"


# --- Reading ---------------------------------------------------------------------------------------------------


def test_each_file_is_read_once_and_only_pdfs(tecc, model):  # noqa: F811
    answers, calls = model
    bill = Document.objects.get(number="T1100011")
    bill.source_id = "x-bill"
    bill.save()
    source = FakeSource({"x-bill": [("T1100011.pdf", "application/pdf", b"%PDF-1"), ("photo.heic", "image/heic", b"x")]})
    answers["T1100011"] = {"service_period": {"from": "2026-09-01", "to": "2026-09-30", "quote": "Sep usage"},
                           "events": [], "lines": []}
    first = read_bill(bill, source)
    again = read_bill(bill, source)
    assert len(calls) == 1 and [r.pk for r in first] == [r.pk for r in again]
    reading = BillReading.objects.get()
    assert reading.file_name == "T1100011.pdf" and len(reading.sha256) == 64 and reading.facts["service_period"]


def test_a_file_that_cant_be_read_is_recorded(tecc, model):  # noqa: F811
    from console.llm import LLMError

    answers, _ = model
    bill = Document.objects.get(number="T1100011")
    bill.source_id = "x-bill"
    bill.save()
    answers["T1100011"] = LLMError("The language model refused the request.")
    reading = read_bill(bill, FakeSource({"x-bill": [("scan.pdf", "application/pdf", b"%PDF")]}))[0]
    assert reading.error.startswith("The language model refused")


# --- The run ---------------------------------------------------------------------------------------------------


def _read(bill, facts):
    BillReading.objects.create(organisation=bill.organisation, document=bill, file_name=f"{bill.number}.pdf",
                               sha256=bill.number.ljust(64, "0"), facts=facts)


def test_the_pdf_moves_a_bill_out_of_the_month(tecc):  # noqa: F811
    scan(tecc.accrual, START, END)
    bill = Document.objects.get(number="T1100011")  # 6 Oct: within CS Energy's usual lag
    _read(bill, {"service_period": {"from": "2026-10-01", "to": "2026-10-31", "quote": "Usage October 2026",
                                    "page": 1}, "events": [], "lines": []})
    journal = run_accruals(tecc.accrual, SEP)
    line = journal.lines.get(account=tecc.power)
    assert line.state == State.PREDICTED  # not September's: estimated instead
    assert any("the PDF says service period from 01 Oct 2026" in s["why"] for s in journal.skipped)


def test_the_pdf_places_a_bill_the_lag_would_miss(tecc):  # noqa: F811
    scan(tecc.accrual, START, END)
    bill = Document.objects.get(number="T1100011")
    bill.date = date(2026, 11, 20)  # 51 days after month end: past the usual lag, inside the bill window
    bill.save()
    _read(bill, {"service_period": {"from": "2026-09-01", "to": "2026-09-30", "quote": "Usage 1-30 Sep 2026",
                                    "page": 2}, "events": [], "lines": []})
    line = run_accruals(tecc.accrual, SEP).lines.get(account=tecc.power)
    assert line.state == State.EVIDENCED and line.expected_amount == D("26000")
    evidence = line.evidence[0]
    assert evidence["placed_by"] == "PDF" and evidence["quote"] == "Usage 1-30 Sep 2026" and evidence["page"] == 2
    assert "per its PDF" in line.basis


def test_reading_job_reads_the_months_bills(tecc, model, monkeypatch):  # noqa: F811
    scan(tecc.accrual, START, END)
    Document.objects.filter(number="T1100011").update(source_id="x-bill", has_attachments=True)
    source = FakeSource({"x-bill": [("T1100011.pdf", "application/pdf", b"%PDF-1")]})
    monkeypatch.setattr("xero.source.source_for", lambda org: source)
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a: date(2026, 10, 20))
    assert jobs.read_bills(tecc.accrual, SEP)
    accrual = Accrual.objects.get(pk=tecc.accrual.pk)
    assert accrual.job_status == jobs.DONE and accrual.job_message == "1 bill(s) after 30 Sep 2026 read."
    assert BillReading.objects.filter(document__number="T1100011").exists()
