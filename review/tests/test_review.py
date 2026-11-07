"""/review: the core P&L checks, working the findings, and review requests (trial run, confirm, run)."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from console.output import Figures, Note, Table
from console.parser import parse
from console.registry import CommandError, Context, lookup
from console.tests.fake_xero import fake_source
from core.models import Account, Contact, Dimension, DimensionValue, Document, DocumentLine, Organisation, Tag
from review import requests as rr
from review.models import CoreCheck, RequestStatus, ReviewFinding, ReviewRequest, ReviewSettings

D = Decimal
TODAY = date(2026, 10, 10)

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    return Organisation.objects.create(name="Demo Events")


@pytest.fixture
def user(org):
    user = get_user_model().objects.create_user("finance", password="pw")
    org.members.add(user)
    return user


@pytest.fixture(autouse=True)
def inline_jobs(monkeypatch):
    monkeypatch.setattr("accounting.jobs.start", lambda work: work())
    monkeypatch.setattr("review.requests.copy_period", lambda *args, **kwargs: None)


def say(org, user, line):
    ctx = Context(org, user, source_factory=lambda _: fake_source(), today_override=TODAY)
    if not line.startswith("/"):
        from console.dialogues import open_dialogue

        module, draft = open_dialogue(ctx)
        return module.answer(ctx, draft, line)
    cmd = parse(line)
    return lookup(cmd.name).func(ctx, cmd)


def tables(result):
    return {b.title: b for b in result.blocks if isinstance(b, Table)}


def notes(result):
    return " ".join(b.text for b in result.blocks if isinstance(b, Note))


def figures(result):
    return {f.label: f.value for b in result.blocks if isinstance(b, Figures) for f in b.items}


def rows(table):
    return {row[1]: row for row in table.rows}


def test_core_checks_default_to_last_full_month(org, user):
    result = say(org, user, "/review")
    assert result.title == "Review: September 2026"
    found = tables(result)
    # Sales 10,000 vs 9,900 last month: under $1,000, so C1 raises nothing for it.
    assert "C1 Month vs last month (over $1,000)" not in found
    assert not any(t.startswith("C2") for t in found)  # the fake's September 2025 is the same
    c3 = rows(found["C3 Month vs budget (over $1,000)"])
    assert c3["200 Sales"][4] == D("-2000.00")  # unfavourable
    assert c3["477 Wages"][4] == D("-3000.00")  # nothing budgeted for September
    assert "469 Rent" not in c3  # 1,000 is not over 1,000
    c5 = tables(result)["C5 COGS % of revenue (over $1,000)"]
    budget_row = next(r for r in c5.rows if r[1] == "COGS % vs budget")
    assert budget_row[2] == D("0.4") and budget_row[3] == 0
    assert "Labour: 477" in notes(result)
    assert "Nothing past materiality in C1" in notes(result)


def test_materiality_and_switches_from_admin(org, user):
    settings = ReviewSettings.for_organisation(org)
    CoreCheck.objects.filter(settings=settings, code="C3").update(amount=D("2500"))
    CoreCheck.objects.filter(settings=settings, code="C2").update(enabled=False)
    CoreCheck.objects.filter(settings=settings, code="C5").update(amount=None, points=D("50"))
    result = say(org, user, "/review --Sep 2026")
    found = tables(result)
    c3 = rows(found["C3 Month vs budget (over $2,500)"])
    assert "477 Wages" in c3 and "200 Sales" not in c3
    assert not any(t.startswith("C2") for t in found)
    assert not any(t.startswith("C5") for t in found)  # 40 points is not over 50


def test_combined_materiality():
    check = CoreCheck(code="C1", amount=D("1000"), percent=D("10"), join="all")
    assert check.material(D("1500"), D("12"))
    assert not check.material(D("1500"), D("5"))
    check.join = "any"
    assert check.material(D("500"), D("12"))
    assert not CoreCheck(code="C1", amount=D("1000")).material(D("-1000"))
    assert CoreCheck(code="C1", amount=D("1000")).material(D("-1000.01"))


def test_work_the_review_and_notes_come_back(org, user):
    result = say(org, user, "/review --Aug 2026")
    c3 = tables(result)["C3 Month vs budget (over $1,000)"]
    sales = next(r for r in c3.rows if r[1] == "200 Sales")
    number = sales[0]
    say(org, user, f"note {number} Expo moved to September")
    say(org, user, "ok 1")
    result = say(org, user, f"why {number}")
    assert "Expo moved" in notes(result)
    assert figures(say(org, user, "refresh"))["Reviewed"] == "2"
    finding = ReviewFinding.objects.get(organisation=org, period_end=date(2026, 8, 31), key="C3:200")
    assert finding.status == "noted" and finding.updated_by == user

    result = say(org, user, "/review --Sep 2026")
    c3 = rows(tables(result)["C3 Month vs budget (over $1,000)"])
    assert 'your note (aug 2026): "expo moved to september"' in c3["200 Sales"][5].lower()

    result = say(org, user, "act 2")
    assert "Next: /" in notes(result)
    assert say(org, user, "done").title.startswith("Review closed")


def test_whole_finished_months_only(org, user):
    with pytest.raises(CommandError, match="hasn't finished"):
        say(org, user, "/review --Oct 2026")
    with pytest.raises(CommandError, match="one whole month"):
        say(org, user, "/review --Q1 FY2027")


# --- Review requests ------------------------------------------------------------------------------------------

SPEC = {
    "title": "Revenue not assigned to an event", "plain": "Every revenue line with no Event, largest first.",
    "cannot": "", "lines": {"categories": ["revenue"], "dimension": "Event", "dimension_filter": "unassigned"},
    "group_by": "line", "measure": "sum", "compare": "none", "raise_when": {}, "action": "/tag",
}


@pytest.fixture
def ledger(org):
    sales = Account.objects.create(organisation=org, code="200", name="Sales", account_class="revenue",
                                   source_type="REVENUE", source_system="xero", source_id="a-sales")
    adf = Contact.objects.create(organisation=org, name="ADF")
    event = Dimension.objects.create(organisation=org, code="event", name="Event")
    expo = DimensionValue.objects.create(organisation=org, dimension=event, code="EXPO", name="ADF Expo")

    def invoice(number, day, amount):
        doc = Document.objects.create(organisation=org, type="invoice", ledger="receivable", number=number,
                                      contact=adf, status="authorised", date=day, line_amount_type="exclusive")
        return DocumentLine.objects.create(organisation=org, document=doc, account=sales, line_amount=D(amount),
                                           description=f"Catering {number}")

    invoice("INV-1", date(2026, 8, 14), "3000.00")
    invoice("INV-2", date(2026, 9, 14), "8420.00")
    tagged = invoice("INV-3", date(2026, 9, 20), "500.00")
    Tag.objects.create(organisation=org, dimension=event, dimension_value=expo, document_line=tagged,
                       allocation=D("1"), method="manual", confidence="high", status="confirmed")
    return event


def test_trial_run_confirm_and_review(org, user, ledger, monkeypatch):
    monkeypatch.setattr(rr, "compile_request", lambda *args: SPEC)
    request = ReviewRequest.objects.create(organisation=org, prompt="iLA, please look at any revenue unassigned "
                                                                    "to any events.")
    assert request.code == "RR-01"
    assert rr.trial_run(request, user, source_factory=lambda _: fake_source(), today=TODAY)
    request.refresh_from_db()
    assert request.status == RequestStatus.TRIAL and request.job_status == "done"
    assert request.title == "Revenue not assigned to an event"
    result = request.trial_result
    assert result["months"] == ["Aug 2026", "Sep 2026"]
    assert [r[0] for r in result["rows"]] == ["Aug 2026", "Sep 2026"]  # the tagged INV-3 isn't there
    assert D(result["total"]) == D("11420.00")
    assert not request.runs

    with pytest.raises(rr.SpecError, match="changed"):
        request.prompt += " Over $100."
        rr.confirm(request, user)
    request.refresh_from_db()
    rr.confirm(request, user)
    assert request.runs and request.status == RequestStatus.CONFIRMED

    review = say(org, user, "/review --Sep 2026")
    table = next(t for t in review.blocks if isinstance(t, Table) and "RR-01" in t.title)
    assert "Revenue not assigned to an event" in table.title and "8,420.00" in table.title
    assert [c.label for c in table.columns][-2:] == ["Event", "Amount"]
    assert table.rows[0][2] == "INV-2" and table.rows[0][-1] == D("8420.00")
    number = int(table.title.split(" ")[0])
    result = say(org, user, f"act {number}")
    assert "Next: /tag" in notes(result)


def test_editing_a_confirmed_request_keeps_the_old_check_running(org, user, ledger, monkeypatch):
    monkeypatch.setattr(rr, "compile_request", lambda *args: SPEC)
    request = ReviewRequest.objects.create(organisation=org, prompt="Unassigned revenue")
    rr.trial_run(request, user, source_factory=lambda _: fake_source(), today=TODAY)
    request.refresh_from_db()
    rr.confirm(request, user)
    request.prompt, request.status = "Unassigned revenue over $10,000", RequestStatus.DRAFT
    request.save()
    assert request.runs
    request.enabled = False
    assert not request.runs


def test_clean_rejects_unusable_checks(org, ledger):
    with pytest.raises(rr.SpecError, match="dimension called"):
        rr.clean(SPEC | {"lines": {"dimension": "Venue"}}, org)
    with pytest.raises(rr.SpecError, match="needs groups"):
        rr.clean(SPEC | {"measure": "ratio", "ratio": {"numerator_codes": ["4061"], "denominator_codes": ["6345"]}},
                 org)
    spec = rr.clean(SPEC | {"group_by": "dimension", "lines": {"dimension": "event"}, "compare": "last_month",
                            "raise_when": {"amount_over": "500"}}, org)
    assert spec["compare"] == "last_month" and spec["raise_when"]["amount_over"] == 500.0


def test_grouped_with_comparison(org, ledger):
    spec = rr.clean(SPEC | {"group_by": "contact", "compare": "last_month", "lines": {"categories": ["revenue"]},
                            "raise_when": {"change_over": 1000}}, org)
    from console.periods import month_period

    result = rr.run_spec(org, spec, [month_period(2026, 9)])
    assert result["columns"][-2:] == [["Last month", "money"], ["Change", "money"]]
    assert result["rows"] == [["ADF", 2, "8920.00", "3000.00", "5920.00"]]


def _form(request):
    request.refresh_from_db()
    return {"organisation": request.organisation_id, "title": request.title, "prompt": request.prompt,
            "trial_months": "", "enabled": "on"}


def test_admin_pages(org, ledger, monkeypatch, client):
    admin_user = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(admin_user)
    monkeypatch.setattr(rr, "compile_request", lambda *args: SPEC)
    request = ReviewRequest.objects.create(organisation=org, prompt="Unassigned revenue")
    rr.trial_run(request, admin_user, source_factory=lambda _: fake_source(), today=TODAY)
    page = client.get(f"/admin/review/reviewrequest/{request.pk}/change/").content.decode()
    assert "Trial run" in page and "Confirm &amp; save" in page and "INV-2" in page
    response = client.post(f"/admin/review/reviewrequest/{request.pk}/change/", _form(request) | {"_confirm": "1"})
    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == RequestStatus.CONFIRMED and request.confirmed_by == admin_user
    settings = ReviewSettings.for_organisation(org)
    page = client.get(f"/admin/review/reviewsettings/{settings.pk}/change/").content.decode()
    assert "C8 Net profit % of revenue" in page
    assert client.get("/admin/review/reviewfinding/").status_code == 200


def test_only_admins_confirm(org, ledger, monkeypatch, client, user):
    user.is_staff = True
    user.save()
    from django.contrib.auth.models import Permission

    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="review").exclude(
        codename="confirm_reviewrequest"))
    client.force_login(user)
    monkeypatch.setattr(rr, "compile_request", lambda *args: SPEC)
    request = ReviewRequest.objects.create(organisation=org, prompt="Unassigned revenue")
    rr.trial_run(request, user, source_factory=lambda _: fake_source(), today=TODAY)
    page = client.get(f"/admin/review/reviewrequest/{request.pk}/change/").content.decode()
    assert "Only an admin can confirm." in page
    response = client.post(f"/admin/review/reviewrequest/{request.pk}/change/", _form(request) | {"_confirm": "1"})
    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == RequestStatus.TRIAL
