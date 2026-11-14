"""Event dates: the value's own start, end, bump days and season, set by /createTag or in admin, suggested from
codes and sales invoices; and new Xero codes tagging lines already copied."""

from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model

from core.models import (
    Account,
    Dimension,
    DimensionValue,
    Document,
    DocumentLine,
    Tag,
    TrackingCategory,
    TrackingOption,
)

from .test_create_tag import client, csv_file, fake, notes, org, say, tables  # noqa: F401  (fixtures)


def admin_client(client):
    boss = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(boss)
    return client


def invoice_line(org, option=None, description="Venue hire", number="INV-1", day=date(2026, 10, 1)):
    account = Account.objects.get_or_create(organisation=org, code="4055", defaults={
        "name": "Rent of Facility", "account_class": Account.AccountClass.REVENUE})[0]
    doc = Document.objects.get_or_create(organisation=org, number=number, defaults={
        "type": Document.Type.INVOICE, "ledger": Document.Ledger.RECEIVABLE, "status": Document.Status.AUTHORISED,
        "date": day})[0]
    line = DocumentLine.objects.create(organisation=org, document=doc, account=account,
                                       line_number=doc.lines.count() + 1, description=description,
                                       line_amount=D("100.00"), source_hash=f"h-{number}-{description}")
    if option:
        line.tracking.add(option)
    return line


def test_create_tag_reads_date_columns(client, org):
    choices = tables(say(client, "/createTag Venue"))["What are you setting up?"]
    say(client, str(len(choices)))
    say(client, "ok")
    bad = [["Code", "Name", "Start date", "End date", "Bump-in days", "Season"],
           ["A", "Arena", "31/10/2026", "30/10/2026", "", ""], ["B", "Ballroom", "x", "", "-1", "maybe"],
           ["C", "Cabaret", "", "01/11/2026", "", ""]]
    text = notes(say(client, "<<", [csv_file(bad)]))
    assert "Row 2 (A): End is before Start" in text and "Row 3 (B): Start must be a date" in text
    assert "Bump-in days must be a whole number from 0 to 60, not -1" in text and "Season must be yes or no" in text
    assert "Row 4 (C): End is given without a Start" in text

    good = [["Code", "Name", "Start date", "End date", "Bump-in days", "Bump-out days", "Season"],
            ["", "Fire season", "01/10/2026", "28/02/2027", "", "", "yes"],
            ["B", "Ballroom", "17/10/2026", "17/10/2026", "0", "1", ""]]
    result = say(client, "<<", [csv_file(good)])
    rows = tables(result)["Values (2)"]
    assert rows[0][:8] == ["261001FIR", "Fire season", "", "01/10/2026", "28/02/2027", "", "", "Yes"]
    say(client, "approve")
    dim = Dimension.objects.get(name="Venue")
    assert dim.attribute_fields == []  # dates are the value's own, not extra fields
    fire, ballroom = dim.values.get(code="261001FIR"), dim.values.get(code="B")
    assert (fire.start_date, fire.end_date, fire.season, fire.is_season) == (date(2026, 10, 1), date(2027, 2, 28),
                                                                          True, True)
    assert (ballroom.start_date, ballroom.end_date, ballroom.bump_in_days, ballroom.bump_out_days) == (
        date(2026, 10, 17), None, 0, 1)

    # Running it again: a changed date shows as Updated, blank cells keep what's saved, and the file has the dates.
    say(client, "/createTag Venue")
    say(client, "1")
    say(client, "ok")
    result = say(client, "<<", [csv_file([["Code", "Name", "Start date"], ["B", "Ballroom", "18/10/2026"],
                                          ["261001FIR", "Fire season", ""]])])
    assert [r[-1] for r in tables(result)["Values (2)"]] == ["Updated", "No change"]
    say(client, "approve")
    ballroom.refresh_from_db()
    assert ballroom.start_date == date(2026, 10, 18) and ballroom.bump_out_days == 1


def test_template_has_dates_and_bump_days(client, org):
    dim = Dimension.objects.create(organisation=org[0], code="venue", name="Venue")
    DimensionValue.objects.create(organisation=org[0], dimension=dim, code="B", name="Ballroom",
                                  start_date=date(2026, 10, 17), end_date=date(2026, 10, 18), bump_in_days=0)
    say(client, "/createTag Venue")
    result = say(client, "1")
    result = say(client, "ok")
    body = client.get(result["link"]).content.decode("utf-8-sig").splitlines()
    assert body == ["Code,Name,Description,Active,Start date,End date,Bump-in days",
                    "B,Ballroom,,yes,17/10/2026,18/10/2026,0"]


def test_approving_a_new_xero_code_tags_lines_already_copied(client, org):
    """The INV-2239 case: lines copied from Xero with an Event code iLA had no value for were left Unassigned.
    Approving /createTag with that code tags them straight away."""
    category = TrackingCategory.objects.create(organisation=org[0], source_system="xero", source_id="tc-event",
                                               name="Event")
    gala = TrackingOption.objects.create(organisation=org[0], source_system="xero", source_id="to-gd",
                                         category=category, name="Gala Dinner")
    line = invoice_line(org[0], gala)
    assert not Tag.objects.exists()

    say(client, "/createTag Event")
    say(client, "ok")
    say(client, "<<", [csv_file([["Code", "Name", "Start date"], ["Gala Dinner", "Gala Dinner 2026", "07/11/2026"]])])
    result = say(client, "approve")
    figures = {b["label"]: b["value"] for block in result["blocks"] if block["type"] == "figures"
               for b in block["items"]}
    assert figures["Transactions re-tagged"] == "1"
    tag = Tag.objects.get(document_line=line)
    assert tag.dimension_value.code == "Gala Dinner" and tag.status == Tag.Status.CONFIRMED


def test_dimension_page_saves_with_many_values_and_links_to_them(client, org):
    client = admin_client(client)
    dim = Dimension.objects.create(organisation=org[0], code="event", name="Event")
    DimensionValue.objects.bulk_create(
        DimensionValue(organisation=org[0], dimension=dim, code=f"E{n}", name=f"Event {n}") for n in range(300))
    page = client.get(f"/admin/core/dimension/{dim.pk}/change/").content.decode()
    assert "300 values</a> (300 without dates)" in page and "values-TOTAL_FORMS" not in page
    form = {"organisation": org[0].pk, "code": "event", "name": "Events", "is_active": "on", "source_name": "",
            "attribute_fields": "[]", "bump_in_days": "3", "bump_out_days": "2", "season_days": "7"}
    response = client.post(f"/admin/core/dimension/{dim.pk}/change/", form)
    assert response.status_code == 302
    dim.refresh_from_db()
    assert dim.name == "Events" and dim.bump_in_days == 3


def test_value_list_edits_dates_and_rejects_duplicates(client, org):
    client = admin_client(client)
    dim = Dimension.objects.create(organisation=org[0], code="event", name="Event")
    arena = DimensionValue.objects.create(organisation=org[0], dimension=dim, code="ARENA", name="Arena")
    form = {"dimension": dim.pk, "code": "arena", "name": "Arena again", "attributes": "{}", "is_active": "on"}
    page = client.post("/admin/core/dimensionvalue/add/", form).content.decode()
    assert "Another value in this dimension already uses this code" in page
    form.update(code="CAB", name="Cabaret", start_date="2026-10-17", end_date="2026-10-16")
    assert "The end can&#x27;t be before the start" in client.post("/admin/core/dimensionvalue/add/", form).content.decode()
    form.update(end_date="2026-10-18")
    assert client.post("/admin/core/dimensionvalue/add/", form).status_code == 302
    cab = DimensionValue.objects.get(code="CAB")
    assert cab.organisation == org[0] and cab.end_date == date(2026, 10, 18)

    changelist = {"form-TOTAL_FORMS": "2", "form-INITIAL_FORMS": "2", "_save": "Save",
                  "form-0-id": arena.pk, "form-0-start_date": "2026-11-01", "form-0-end_date": "",
                  "form-0-bump_in_days": "1", "form-0-bump_out_days": "", "form-0-is_active": "on",
                  "form-1-id": cab.pk, "form-1-start_date": "2026-10-17", "form-1-end_date": "2026-10-18",
                  "form-1-bump_in_days": "", "form-1-bump_out_days": "", "form-1-is_active": "on"}
    response = client.post(f"/admin/core/dimensionvalue/?dimension__id__exact={dim.pk}", changelist)
    assert response.status_code == 302
    arena.refresh_from_db()
    assert arena.start_date == date(2026, 11, 1) and arena.bump_in_days == 1


def test_suggest_dates_from_codes_and_invoices_then_confirm(client, org):
    client = admin_client(client)
    category = TrackingCategory.objects.create(organisation=org[0], source_system="xero", source_id="tc-event",
                                               name="Event")
    opts = {n: TrackingOption.objects.create(organisation=org[0], source_system="xero", source_id=f"to-{n}",
                                             category=category, name=n) for n in ("261008MEL", "CCH1221", "VAGUE")}
    dim = Dimension.objects.create(organisation=org[0], code="event", name="Event", tracking_category=category)
    mel, cch, vague, dated = (
        DimensionValue.objects.create(organisation=org[0], dimension=dim, code=c, name=c, tracking_option=opts.get(c),
                                      start_date=date(2026, 1, 1) if c == "KEEP" else None)
        for c in ("261008MEL", "CCH1221", "VAGUE", "KEEP"))
    invoice_line(org[0], opts["261008MEL"], "Venue Hire - Arena - 09.10.26", "INV-1", date(2026, 9, 1))
    invoice_line(org[0], opts["CCH1221"], "Concert hall 12/12/2021 and 13/12/2021", "INV-2", date(2021, 11, 1))
    invoice_line(org[0], opts["VAGUE"], "Deposit 01/03/2026, balance 30/06/2026", "INV-3", date(2026, 2, 1))

    ids = [str(v.pk) for v in (mel, cch, vague, dated)]
    page = client.post("/admin/core/dimensionvalue/", {"action": "suggest_dates", "_selected_action": ids})
    text = page.content.decode()
    assert "Suggested dates" in text and "1 selected value(s) without dates had nothing to go on" in text
    assert "08/10/2026" in text and "09/10/2026" in text and "Code, and dates on its sales invoices" in text
    assert "12/12/2021" in text and "13/12/2021" in text

    response = client.post("/admin/core/dimensionvalue/", {"action": "suggest_dates", "_selected_action": ids,
                                                           "apply": "1", "use": [str(mel.pk)]})
    assert response.status_code == 302
    for v in (mel, cch, vague, dated):
        v.refresh_from_db()
    assert (mel.start_date, mel.end_date) == (date(2026, 10, 8), date(2026, 10, 9))
    assert cch.start_date is None and vague.start_date is None  # unticked, and too far apart to guess
    assert dated.start_date == date(2026, 1, 1)  # values with dates are never changed


def test_linking_a_value_to_xero_in_admin_tags_copied_lines(client, org):
    client = admin_client(client)
    category = TrackingCategory.objects.create(organisation=org[0], source_system="xero", source_id="tc-event",
                                               name="Event")
    fire = TrackingOption.objects.create(organisation=org[0], source_system="xero", source_id="to-fire",
                                         category=category, name="2627FIRE")
    dim = Dimension.objects.create(organisation=org[0], code="event", name="Event", tracking_category=category)
    line = invoice_line(org[0], fire)
    form = {"dimension": dim.pk, "code": "2627FIRE", "name": "Townsville Fire Games 26-27 season",
            "tracking_option": fire.pk, "start_date": "2026-10-01", "end_date": "2027-02-28", "attributes": "{}",
            "is_active": "on"}
    assert client.post("/admin/core/dimensionvalue/add/", form).status_code == 302
    assert Tag.objects.get(document_line=line).dimension_value.code == "2627FIRE"
