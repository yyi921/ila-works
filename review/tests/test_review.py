"""/review: the P&L by group (L0) and GL accounts (L1), notes and sign-offs, and review requests (trial run,
confirm, run)."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from console.output import Figures, Note, Points, Table
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


def l0(result):
    return rows(tables(result)["P&L by group"])


def test_l0_by_group_against_last_month(org, user):
    result = say(org, user, "/review")
    assert result.title == "Review: September 2026"
    assert "MTD · vs Last month (Aug 2026)" in result.subtitle
    found = l0(result)
    assert list(found) == ["Revenue", "Cost of sales", "Gross profit", "Other income", "Labour",
                           "Other operating costs", "EBITDA", "Net profit", "Gross profit %", "COGS %", "Labour %",
                           "EBITDA %", "Net profit %"]
    # Ref, line, actual, compare, var $, var %, notes, status
    assert found["Revenue"][:6] == ["rev", "Revenue", D("10000.00"), D("10000.00"), D("0.00"), D("0")]
    assert found["Labour"][2] == D("3000.00") and found["Labour"][7] == "Open"
    assert found["Other operating costs"][2] == D("1050.00")  # rent plus the 50.00 not on an account row
    assert found["Net profit"][2] == D("2450.00")  # ties to Xero
    assert found["Labour %"][2] == D("0.3") and found["Labour %"][4] is None
    assert "reading" not in str(result.to_json())
    assert "Labour: 477" in notes(result)


def test_switch_comparison_window_and_level(org, user):
    say(org, user, "/review --Sep 2026")
    found = l0(say(org, user, "vs ly"))
    assert found["Revenue"][3:6] == [D("8000.00"), D("2000.00"), D("0.25")]
    assert isinstance(found["Gross profit %"][5], Points) and found["Gross profit %"][5] == D("-2.5")  # 60.0% vs 62.5%
    result = say(org, user, "ytd")
    assert "YTD · vs Last year YTD (2025)" in result.subtitle
    result = say(org, user, "vs budget")
    assert "Budget YTD" in [c.label for c in tables(result)["P&L by group"].columns]
    result = say(org, user, "L1 rev cost of sales")
    assert list(tables(result)) == ["Revenue (rev) · GL accounts", "Cost of sales (cogs) · GL accounts"]
    assert tables(result)["Revenue (rev) · GL accounts"].rows[0][:2] == ["200", "Sales"]
    with pytest.raises(CommandError, match="last month doesn't mean"):
        say(org, user, "vs lm")
    result = say(org, user, "/review --lab --L1")
    assert list(tables(result)) == ["Labour (lab) · GL accounts"]
    with pytest.raises(CommandError, match="doesn't mean anything"):
        say(org, user, "/review --ytd --vs lm")


def test_notes_sign_off_reopen_and_changes(org, user):
    say(org, user, "/review --Sep 2026")
    say(org, user, "note 200 Expo moved to October")
    say(org, user, "note rev Check the Expo deposit")
    result = say(org, user, "signoff rev 477")
    found = l0(result)
    assert found["Revenue"][6] == 2 and found["Revenue"][7].startswith("✓ Signed off · finance")
    assert tables(result)["P&L by group"].tones[0] == "ok"
    assert figures(result)["Groups signed off"] == "2 of 5"  # revenue, and labour through its only account
    point = ReviewFinding.objects.get(organisation=org, period_end=date(2026, 9, 30), key="200")
    assert point.status == "signed_off" and point.signed_amount == D("10000.00") and point.signed_by == user
    assert point.notes[0].startswith("Expo moved to October — finance ")

    assert l0(say(org, user, "reopen 200"))["Revenue"][7] == "Open"

    # A later refresh finds wages moved: the sign-off shows as changed until it's signed off again.
    from console.models import ReviewSession

    session = ReviewSession.objects.get(organisation=org, status="open")
    for line in session.data["lines"]:
        if line["code"] == "477":
            line["values"]["cur"] = "3600.00"
    session.save()
    result = say(org, user, "L0")
    assert l0(result)["Labour"][7] == "⚠ Changed since sign-off (+600.00)"
    assert tables(result)["P&L by group"].tones[4] == "warn"
    assert figures(result)["Changed since sign-off"] == "1"
    assert l0(say(org, user, "signoff 477"))["Labour"][7].startswith("✓ Signed off")

    assert [r[1] for r in say(org, user, "notes").tables()[0].rows] == ["200", "rev"]
    with pytest.raises(CommandError, match="isn't an account code"):
        say(org, user, "signoff 9999")
    say(org, user, "clear 200")
    assert len(say(org, user, "notes").tables()[0].rows) == 1

    ctx = Context(org, user, source_factory=lambda _: fake_source(), today_override=TODAY)
    review = tables(lookup("status").func(ctx, parse("/status")))["Month-end review: September 2026"]
    assert ["Groups signed off", "1 of 5"] in review.rows  # 200 was reopened, so revenue isn't signed off
    assert say(org, user, "done").title.startswith("Review closed")


def test_old_findings_fold_into_review_points(org):
    from importlib import import_module

    from django.apps import apps

    month = date(2026, 8, 31)
    for key, status, note in (("C1:200", "noted", "Expo moved"), ("C3:200", "ok", "Expo moved"),
                              ("C3:477", "noted", "Pay run late"), ("C6:last_month", "noted", "Ratio"),
                              ("RR-01", "later", "")):
        ReviewFinding.objects.create(organisation=org, period_end=month, key=key, subject=key, status=status,
                                     note=note)
    import_module("review.migrations.0002_review_points").fold_findings(apps, None)
    points = {p.key: p for p in ReviewFinding.objects.filter(organisation=org)}
    assert set(points) == {"200", "477"}
    assert points["200"].note == "Expo moved" and points["200"].status == "open"


def test_combined_materiality():
    check = CoreCheck(code="C1", amount=D("1000"), percent=D("10"), join="all")
    assert check.material(D("1500"), D("12"))
    assert not check.material(D("1500"), D("5"))
    check.join = "any"
    assert check.material(D("500"), D("12"))
    assert not CoreCheck(code="C1", amount=D("1000")).material(D("-1000"))
    assert CoreCheck(code="C1", amount=D("1000")).material(D("-1000.01"))


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
    assert table.title == "RR-01 Revenue not assigned to an event · Open"
    assert [c.label for c in table.columns][-2:] == ["Event", "Amount"]
    assert table.rows[0][2] == "INV-2" and table.rows[0][-1] == D("8420.00")
    say(org, user, "note rr-1 Tag INV-2 to the Expo")
    review = say(org, user, "signoff RR-01")
    table = next(t for t in review.blocks if isinstance(t, Table) and "RR-01" in t.title)
    assert "✓ Signed off" in table.title and "Notes: Tag INV-2 to the Expo" in table.title
    assert figures(review)["Review requests signed off"] == "1 of 1"


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
    assert "Labour accounts" in page and "C8 Net profit" not in page and "cogs / rev" in page
    point = ReviewFinding.objects.create(organisation=org, period_end=date(2026, 9, 30), key="200", subject="200",
                                         note="Expo moved", history=[{"at": "2026-11-08T10:00", "by": "boss",
                                                                      "what": "note", "note": "Expo moved"}])
    assert client.get("/admin/review/reviewfinding/").status_code == 200
    assert "Expo moved" in client.get(f"/admin/review/reviewfinding/{point.pk}/change/").content.decode()


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


# --- Ratios ----------------------------------------------------------------------------------------------------

def test_ratio_formulas_parse():
    from review.ratios import FormulaError, parse

    top, bottom = parse("Sum of GL5110 - GL 5330 / (GL 4005 + GL4810, 4830)")
    assert top == [(1, "range", "5110-5330")]
    assert bottom == [(1, "code", "4005"), (1, "code", "4810"), (1, "code", "4830")]
    assert parse("rev less cogs minus 5110 / rev")[0] == [(1, "group", "rev"), (-1, "group", "cogs"),
                                                          (-1, "code", "5110")]
    for bad, message in (("lab", "one /"), ("lab / rev / 4", "one /"), ("rev - cogs / rev", "write less"),
                         ("lab + / rev", "empty term"), ("wages / rev", "isn't an account code")):
        with pytest.raises(FormulaError, match=message):
            parse(bad)


def test_custom_ratios_in_review(org, user):
    from review.models import ReviewRatio

    settings = ReviewSettings.for_organisation(org)
    assert [r.formula for r in settings.ratios.all()] == ["gp / rev", "cogs / rev", "lab / rev", "ebitda / rev",
                                                          "np / rev"]
    ReviewRatio.objects.filter(settings=settings, name="COGS %").update(formula="310 / 200 + 260")
    ReviewRatio.objects.filter(settings=settings, name="EBITDA %").update(enabled=False)
    ReviewRatio.objects.create(organisation=org, settings=settings, name="Rent and wages %",
                               formula="GL469 - GL477 / rev", order=9)
    found = l0(say(org, user, "/review --Sep 2026"))
    assert found["COGS %"][2] == D("4000") / D("10500")
    assert "EBITDA %" not in found
    assert found["Rent and wages %"][2] == D("0.4")  # 469 and 477: 1,000 + 3,000 over 10,000
    assert "COGS % = 310 / 200 + 260" in notes(say(org, user, "L0"))


def test_ratio_codes_checked_against_the_chart(org, ledger):
    from django.core.exceptions import ValidationError

    from review.models import ReviewRatio

    settings = ReviewSettings.for_organisation(org)
    ratio = ReviewRatio(organisation=org, settings=settings, name="X", formula="999 / 200")
    with pytest.raises(ValidationError, match="Not in the chart of accounts: 999"):
        ratio.full_clean()
    ratio.formula = "100-299 / 200"
    ratio.full_clean()
    ratio.formula = "200 / "
    with pytest.raises(ValidationError, match="empty term"):
        ratio.full_clean()


def test_edit_ratios_in_admin(org, ledger, client):
    from review.models import ReviewRatio

    client.force_login(get_user_model().objects.create_superuser("boss", password="pw"))
    settings = ReviewSettings.for_organisation(org)
    existing = list(settings.ratios.all())
    form = {"organisation": org.pk, "trial_months": "2", "pack_view": "landing",
            "ratios-TOTAL_FORMS": str(len(existing) + 1), "ratios-INITIAL_FORMS": str(len(existing)),
            "ratios-MIN_NUM_FORMS": "0", "ratios-MAX_NUM_FORMS": "1000"}
    for i, r in enumerate(existing):
        form |= {f"ratios-{i}-id": r.pk, f"ratios-{i}-settings": settings.pk, f"ratios-{i}-order": r.order,
                 f"ratios-{i}-name": r.name, f"ratios-{i}-formula": r.formula, f"ratios-{i}-enabled": "on"}
    new = len(existing)
    form |= {f"ratios-1-formula": "GL200 / 200", f"ratios-{new}-settings": settings.pk, f"ratios-{new}-order": "9",
             f"ratios-{new}-name": "Sales share %", f"ratios-{new}-formula": "200 / rev", f"ratios-{new}-enabled": "on"}
    url = f"/admin/review/reviewsettings/{settings.pk}/change/"
    response = client.post(url, form)
    assert response.status_code == 302, response.content.decode()[:3000]
    assert ReviewRatio.objects.get(settings=settings, name="COGS %").formula == "GL200 / 200"
    assert ReviewRatio.objects.get(settings=settings, name="Sales share %").organisation == org
    form[f"ratios-{new}-formula"] = "999 / rev"
    form[f"ratios-{new}-name"] = "Bad"
    assert "Not in the chart of accounts: 999" in client.post(url, form).content.decode()
