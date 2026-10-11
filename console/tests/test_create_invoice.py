"""/_createInvoice end to end, with the fake Xero organisation and a stand-in for the language model."""

import csv
import io
import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from console import llm
from console.models import CustomCommand, InvoiceDraft
from core.models import AuditLog, ImportFile, ImportFormat, InvoiceItem, InvoiceSetup, Organisation

from .fake_xero import fake_source

FACTS = {"document_type": "agreement", "title": "Hire agreement", "summary": "Venue hire for Celtic Nights.",
         "prices_include_tax": "inclusive", "charges": [{"description": "Venue Hire", "amount": "4400"}]}


def first_draft():
    return {"header": {"reference": "CELTIC NIGHTS 2026", "date": "2026-04-30"},
            "lines": [
                {"item": "Venue Hire - Arena", "description": "Venue Hire - Arena", "quantity": 1,
                 "unit_amount": 4400, "source": "agreement.pdf p1"},
                {"item": "", "description": "Security", "quantity": 21, "unit_amount": 85,
                 "source": "agreement.pdf p1"},
                {"item": "First Aid", "description": "First Aid", "quantity": 10, "unit_amount": 100,
                 "source": "agreement.pdf p2"},
                {"item": "", "description": "Catering", "quantity": 120, "unit_amount": 0, "source": "order p3"}],
            "payments_to_apply": [{"description": "Deposit", "amount": 2200, "source": "agreement.pdf p1"}],
            "assumptions": ["Followed the event order where it differs from the agreement."],
            "questions": ["Were complimentary tickets issued ($0.50 each)?"]}


class FakeModel:
    def __init__(self):
        self.calls = []

    def __call__(self, *, system, content, tool_name, description, schema):
        self.calls.append((tool_name, content))
        if tool_name == "record_document":
            return FACTS
        context = json.loads(content[0]["text"])
        proposal = first_draft()
        if "current_draft" in context:
            proposal["lines"][1]["quantity"] = 24
            proposal["changes"] = ["Security: quantity 21 → 24"]
            if "number" in content[1]["text"]:
                proposal["header"]["number"] = "INV-1"
        return proposal


@pytest.fixture
def model(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = "test-key"
    fake = FakeModel()
    monkeypatch.setattr(llm, "call_tool", fake)
    return fake


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("finance", password="pw")


@pytest.fixture
def org(user):
    org = Organisation.objects.create(name="TECC")
    org.members.add(user)
    return org


@pytest.fixture
def setup(org):
    setup = InvoiceSetup.objects.create(
        organisation=org, name="Event invoices", import_format=ImportFormat.objects.get(name="Xero sales invoice CSV"),
        tax_basis="inclusive", default_tax_type="GST on Income", default_account_code="200", tracking_name="Event", due_days=14,
        sources="Hire agreement\nEvent order")
    InvoiceItem.objects.create(setup=setup, name="Venue Hire - Arena", account_code="200", rate=Decimal("4400"))
    InvoiceItem.objects.create(setup=setup, name="First Aid", aliases="First aiders", unit="hour",
                               rate=Decimal("100"), tax_type="GST Free Income")
    return setup


@pytest.fixture
def command(org):
    return CustomCommand.objects.create(organisation=org, name="_createInvoice", template="createinvoice",
                                        summary="Invoices")


@pytest.fixture
def client(client, user):
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    source = fake_source()
    monkeypatch.setattr("console.views.source_for", lambda organisation: source)
    return source


def say(client, text, files=()):
    data = client.post("/console/run/", {"command": text, "file": list(files)}).json()
    assert data["ok"], data
    return data["result"]


def notes(result):
    return " ".join(b["text"] for b in result["blocks"] if b["type"] == "note")


def table(result, title=""):
    return next(b for b in result["blocks"] if b["type"] == "table" and b["title"] == title)


def pdf(name="agreement.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 fake", content_type="application/pdf")


def test_full_conversation_builds_a_stamped_import_file(client, org, setup, command, model, fake):
    result = say(client, "/_createinvoice harbour")  # name typed in any case; text searches customers
    assert result["subtitle"] == "Step: Customer"
    assert table(result, "Pick one")["rows"][0]["cells"][1].startswith("Harbour Events Pty Ltd")

    result = say(client, "1")
    assert result["subtitle"] == "Step: Event or job"
    assert table(result, "Pick one")["rows"][0]["cells"] == ["1", "CELTIC NIGHTS 2026"]

    result = say(client, "celtic nights")  # matches the Event tracking option exactly
    assert result["subtitle"] == "Step: Billing information" and "Hire agreement; Event order" in notes(result)
    draft = InvoiceDraft.objects.get()
    assert draft.data["event"] == {"name": "celtic nights", "tracking_option": "Celtic Nights"}

    result = say(client, "<< agreement.pdf", [pdf()])
    assert table(result, "Billing information")["rows"][0]["cells"][:2] == ["agreement.pdf", "agreement"]
    assert model.calls[0][1][0]["type"] == "document"

    result = say(client, "done")
    assert result["subtitle"] == "Step: Review"
    lines = table(result, "Lines")["rows"]
    venue, security, first_aid, catering = (r["cells"] for r in lines[:4])
    assert venue[6] == "200 (item list)" and venue[7] == "GST on Income" and venue[9] == ""
    assert security[6] == "260 (as on INV-2070)" and "not in the item list" in security[9]
    assert first_aid[7] == "GST Free Income"
    assert "$0 line" in catering[9] and catering[6] == "200 (set-up default)"
    # Inclusive: 4400 + 1785 + 1000 = 7185 total; GST = 4400/11 + 1785/11 = 400.00 + 162.27
    assert [r["cells"][5] for r in lines[4:]] == ["6,622.73", "562.27", "7,185.00"]
    figures = next(b for b in result["blocks"] if b["type"] == "figures")["items"]
    assert figures[0]["value"] == "INV-2071" and figures[1]["value"].startswith("30 Apr 2026 · due 14 May 2026")
    assert "complimentary tickets" in notes(result)
    assert table(result, "Payments to apply in Xero (not invoice lines)")["rows"][-1]["cells"][1] == "4,985.00"

    result = say(client, "security was 24 hours")
    assert "Security: quantity 21 → 24" in notes(result)

    result = say(client, "approve")
    assert result["title"] == "Approved: INV-2071 for Harbour Events Pty Ltd"
    assert "Tax inclusive" in notes(result) and "Catering" in notes(result)
    f = ImportFile.objects.get()
    assert f.status == ImportFile.Status.STAMPED and f.stamp_is_valid(bytes(f.content))
    assert result["link"] == f"/console/files/{f.pk}/download/"
    rows = list(csv.DictReader(io.StringIO(bytes(f.content).decode())))
    assert [r["*Description"] for r in rows] == ["Venue Hire - Arena", "Security", "First Aid", "Catering"]
    assert rows[1]["*Quantity"] == "24" and rows[1]["*AccountCode"] == "260"
    assert rows[0]["*InvoiceDate"] == "30/04/2026" and rows[0]["EmailAddress"] == "accounts@harbour.test"
    assert rows[0]["TrackingName1"] == "Event" and rows[0]["TrackingOption1"] == "Celtic Nights"
    draft.refresh_from_db()
    assert draft.status == InvoiceDraft.Status.APPROVED and draft.import_file == f
    assert AuditLog.objects.filter(action=AuditLog.Action.APPROVAL, target_id=str(f.pk)).exists()


def reach_review(client, model):
    say(client, "/_createInvoice harbour")
    say(client, "1")
    say(client, "celtic nights")
    say(client, "8 hours on 1 October at $80")  # typed details count as billing information
    return say(client, "done")


def test_approval_is_blocked_until_problems_are_fixed(client, org, setup, command, model):
    setup.default_tax_type = "Old Rate"
    setup.save()
    InvoiceItem.objects.filter(name="First Aid").update(tax_type="")
    reach_review(client, model)
    say(client, "number please INV-1")
    result = say(client, "approve")
    text = notes(result)
    assert "INV-1 is already used in Xero" in text and "tax rate Old Rate isn't active" in text
    assert not ImportFile.objects.exists()
    assert InvoiceDraft.objects.get().status == InvoiceDraft.Status.OPEN


def test_typed_details_are_kept_as_a_source(client, org, setup, command, model):
    result = reach_review(client, model)
    assert table(result, "Billing information")["rows"][0]["cells"] == ["Typed by you", "note",
                                                                        "8 hours on 1 October at $80"]


def test_resume_cancel_and_start_again(client, org, setup, command, model):
    say(client, "/_createInvoice harbour")
    result = say(client, "/_createInvoice")
    assert "Picking up your open draft" in notes(result) and result["subtitle"] == "Step: Customer"
    say(client, "/_createInvoice --new")
    assert InvoiceDraft.objects.filter(status="cancelled").count() == 1
    assert say(client, "/cancel")["title"] == "Draft cancelled"
    data = client.post("/console/run/", {"command": "hello"}).json()
    assert not data["ok"] and 'start with "/"' in data["error"]


def test_choosing_between_setups_and_new_customers(client, org, setup, command, model):
    InvoiceSetup.objects.create(organisation=org, name="Labour hire", import_format=setup.import_format)
    result = say(client, "/_createInvoice")
    assert result["subtitle"] == "Step: Choose the invoice set-up" and len(table(result, "Pick one")["rows"]) == 2
    assert say(client, "2")["title"] == "/_createInvoice · Labour hire"
    result = say(client, "Zebra Events")
    assert "No customer in Xero matches" in notes(result)
    result = say(client, "1")
    assert table(result)["rows"][0]["cells"] == ["Customer", "Zebra Events (new)"]


def test_without_a_setup_or_the_model(client, org, command, setup, settings):
    settings.ANTHROPIC_API_KEY = ""
    say(client, "/_createInvoice harbour")
    say(client, "1")
    result = say(client, "gala dinner")
    assert "isn't set up on this server yet" in notes(result)
    data = client.post("/console/run/", {"command": "<< a.pdf", "file": [pdf("a.pdf")]}).json()
    assert not data["ok"] and "ANTHROPIC_API_KEY" in data["error"]
    InvoiceSetup.objects.all().delete()
    data = client.post("/console/run/", {"command": "/_createInvoice --new"}).json()
    assert "has no invoice set-up yet" in data["error"]


def test_help_explains_the_dialogue(client, org, command):
    result = say(client, "/help _createinvoice")
    assert "guided conversation" in notes(result)


def test_plain_go_ahead_drafts_with_assumptions(client, org, setup, command, model):
    say(client, "/_createInvoice harbour")
    say(client, "1")
    say(client, "celtic nights")
    say(client, "<< agreement.pdf", [pdf()])
    result = say(client, "Go ahead and create a draft to your best knowledge, showing sources and assumptions")
    assert result["subtitle"] == "Step: Review"
    assert "Assumed: Followed the event order" in notes(result)
    assert "The person says: Go ahead" in model.calls[-1][1][1]["text"]
    assert len(InvoiceDraft.objects.get().data["sources"]) == 1  # the request isn't kept as billing information


def test_typed_details_with_numbers_are_not_taken_as_go_ahead(client, org, setup, command, model):
    say(client, "/_createInvoice harbour")
    say(client, "1")
    say(client, "celtic nights")
    result = say(client, "Prepare 8 hours on 1 October")
    assert result["subtitle"] == "Step: Billing information"
