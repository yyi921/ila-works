import csv
import io

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from console.models import TagDraft
from core.models import AuditLog, Dimension, DimensionValue, Organisation, TrackingCategory, TrackingOption

from .fake_xero import fake_source


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


def tables(result):
    return {b["title"]: [r["cells"] for r in b["rows"]] for b in result["blocks"] if b["type"] == "table"}


def csv_file(rows, name="values.csv"):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return SimpleUploadedFile(name, out.getvalue().encode("utf-8-sig"), content_type="text/csv")


EVENT_ROWS = [
    ["Code", "Name", "Description", "Venue", "Event date", "Capacity", "Ticketed"],
    ["celtic nights", "Celtic Nights 2026", "Irish dance show", "Arena", "14/03/2026", "4,500", "yes"],
    ["XMAS26", "Christmas Party", "", "Ballroom", "2026-12-12", "300", "no"],
]


def test_choices_offer_xero_categories_and_a_new_dimension(client):
    result = say(client, "/createTag")
    choices = tables(result)["What are you setting up?"]
    assert choices[0][1] == "Replace Xero's Event tracking (2 active options)"
    assert choices[-1][1] == "A new dimension (not in Xero)"
    assert "keeps Xero exactly as it is" in notes(result)


def test_replace_xero_category_end_to_end(client, org):
    result = say(client, "/createtag event")  # name matches the Xero category: straight to the name step
    assert "replaces Xero's Event tracking category" in notes(result)
    result = say(client, "ok")
    assert result["link"].endswith("/template/")
    assert "Code" in [r[0] for r in tables(result)["The values file (CSV)"]]

    result = say(client, "<< events.csv", [csv_file(EVENT_ROWS)])
    rows = tables(result)["Values (2)"]
    assert rows[0] == ["Celtic Nights", "Celtic Nights 2026", "Irish dance show", "Yes", "Arena", "14/03/2026",
                       "4500", "Yes", "Yes", "New"]
    assert rows[1][3] == "No"  # XMAS26 isn't a Xero option
    text = notes(result)
    assert "XMAS26" in text and "iLA-only" in text
    assert "Gala Dinner" in text  # active Xero option missing from the file
    assert "Event date (a date" in text and "Capacity (a number)" in text and "Ticketed (yes or no)" in text
    assert not Dimension.objects.exists()  # nothing saved before approval

    result = say(client, "approve")
    assert result["title"] == "Saved: Event"
    dim = Dimension.objects.get()
    assert dim.replaces_source and dim.source_name == "Event" and dim.code == "event"
    assert dim.tracking_category.source_id == "tc-event"
    values = {v.code: v for v in dim.values.select_related("tracking_option")}
    assert set(values) == {"Celtic Nights", "XMAS26", "Gala Dinner"}
    cn = values["Celtic Nights"]
    assert cn.name == "Celtic Nights 2026" and cn.tracking_option.source_id == "to-cn"
    assert cn.attributes == {"venue": "Arena", "event_date": "2026-03-14", "capacity": 4500, "ticketed": True}
    assert values["XMAS26"].tracking_option is None
    assert values["Gala Dinner"].name == "Gala Dinner" and values["Gala Dinner"].tracking_option.source_id == "to-gd"
    assert TrackingOption.objects.filter(source_id="to-os", is_active=False).exists()
    assert TagDraft.objects.get().status == TagDraft.Status.APPROVED
    log = AuditLog.objects.get(action=AuditLog.Action.APPROVAL)
    assert log.result["created"] == 2 and log.result["from_xero"] == 1


def test_rerun_updates_the_dimension_that_replaced_xero(client, org):
    say(client, "/createTag Event")
    say(client, "ok")
    say(client, "<<", [csv_file(EVENT_ROWS)])
    say(client, "approve")

    result = say(client, "/createTag")
    assert "updates iLA dimension Event" in tables(result)["What are you setting up?"][0][1]
    say(client, "1")
    say(client, "Events")  # rename
    rows = [["Code", "Name", "Venue"], ["Celtic Nights", "Celtic Nights 2026", "Main arena"]]
    result = say(client, "<<", [csv_file(rows)])
    assert tables(result)["Values (1)"][0][-1] == "Updated"
    assert "aren't in the file and stay as they are" in notes(result)
    say(client, "approve")
    dim = Dimension.objects.get()
    assert dim.name == "Events" and TrackingCategory.objects.count() == 1
    assert dim.values.get(code="Celtic Nights").attributes["venue"] == "Main arena"
    assert dim.values.get(code="Celtic Nights").attributes["capacity"] == 4500  # blank/missing cells keep values


def test_new_dimension_reports_file_problems_then_accepts_a_fix(client, org):
    say(client, "/createTag Venue")
    choices = TagDraft.objects.get().data["choices"]
    assert choices[-1]["label"] == 'A new dimension "Venue" (not in Xero)'
    result = say(client, str(len(choices)))
    assert "brand-new iLA dimension" in notes(result) and '"Venue"' in notes(result)
    say(client, "ok")

    result = say(client, "<<", [csv_file([["Code", "Title"], ["A", "Arena"]])])
    assert 'needs a "Name" column' in notes(result)
    bad = [["Code", "Name", "Active", "Seats"], ["A", "Arena", "maybe", "5000"], ["A", "Again", "", "x"],
           ["", "No code", "", ""], ["B", "", "", ""]]
    result = say(client, "<<", [csv_file(bad)])
    text = notes(result)
    assert "Active must be yes or no" in text and "Code A is also on row 2" in text
    assert "Row 5 (B): Name is empty" in text
    assert "made one from the date and name: NOCODE" in TagDraft.objects.get().data["warnings"][0]
    assert "Nothing to approve" in notes(say(client, "approve"))

    result = say(client, "<<", [csv_file([["Code", "Name", "Seats"], ["A", "Arena", "5000"], ["B", "Ballroom", ""]])])
    assert result["subtitle"] == "Step: Review"
    say(client, "approve")
    dim = Dimension.objects.get(name="Venue")
    assert not dim.replaces_source and dim.tracking_category is None
    assert dim.attribute_fields == [{"key": "seats", "label": "Seats", "type": "number", "required": False}]
    assert dim.values.get(code="A").attributes == {"seats": 5000}


def test_template_has_xero_codes_to_fill_in(client, org):
    say(client, "/createTag Event")
    result = say(client, "ok")
    response = client.get(result["link"])
    body = response.content.decode("utf-8-sig")
    assert response["Content-Disposition"] == 'attachment; filename="Event values.csv"'
    assert body.splitlines() == ["Code,Name,Description,Active", "Celtic Nights,,,yes", "Gala Dinner,,,yes",
                                 "Old Show,,,no"]
    other = get_user_model().objects.create_user("other", password="pw")
    org[0].members.add(other)
    client.force_login(other)
    assert client.get(result["link"]).status_code == 404


def test_cancel_and_name_clash(client, org):
    Dimension.objects.create(organisation=org[0], code="dept", name="Department")
    say(client, "/createTag")
    say(client, str(len(TagDraft.objects.get().data["choices"])))  # new
    assert "already a dimension called" in notes(say(client, "department"))
    result = say(client, "/cancel")
    assert result["title"] == "Tag set-up cancelled"
    assert TagDraft.objects.get().status == TagDraft.Status.CANCELLED


def test_dimension_that_replaces_needs_the_source_name(org):
    dim = Dimension(organisation=org[0], code="event", name="Event", replaces_source=True)
    with pytest.raises(ValidationError, match="accounting system calls"):
        dim.full_clean()
    dim.source_name = "Events Department"
    dim.full_clean()


def test_empty_codes_are_made_from_date_and_name(client, org):
    say(client, "/createTag Event")
    say(client, "ok")
    rows = [["Code", "Name", "Start date"],
            ["", "Fire Game 1", "25/10/2026"],
            ["261025FIR", "Fire Game warm-up", "25/10/2026"],  # an explicit code wins; the made one moves on
            ["", "Gala Dinner", ""],  # no date: the name's letters
            ["", "Celtic Nights", "14/03/2026"]]
    result = say(client, "<<", [csv_file(rows)])
    codes = [r[0] for r in tables(result)["Values (4)"]]
    assert codes == ["261025FIR2", "261025FIR", "GALADINNER", "260314CEL"]
    text = notes(result)
    assert "4 row(s) had no Code" not in text and "3 row(s) had no Code" in text
    say(client, "approve")
    assert Dimension.objects.get().values.filter(code="261025FIR2", name="Fire Game 1").exists()


def test_make_code():
    from console.create_tag import make_code

    assert make_code("Department of Education", "2026-08-05", set()) == "260805DEP"
    assert make_code("ACRRM", None, set()) == "ACRRM"
    assert make_code("St Margaret Mary's Formal", None, {"stmargaret"}) == "STMARGARET2"
    assert make_code("!!", None, set()) == "VALUE"


def test_codes_are_unique_ignoring_case(org):
    dim = Dimension.objects.create(organisation=org[0], code="event", name="Event")
    with pytest.raises(ValidationError, match="Another dimension already uses this code"):
        Dimension(organisation=org[0], code="EVENT", name="Events").full_clean()
    DimensionValue.objects.create(organisation=org[0], dimension=dim, code="CCH1221", name="Concert")
    with pytest.raises(ValidationError, match="Another value in this dimension already uses this code"):
        DimensionValue(organisation=org[0], dimension=dim, code="cch1221", name="Copy").full_clean()
    other = Dimension.objects.create(organisation=org[0], code="venue", name="Venue")
    DimensionValue(organisation=org[0], dimension=other, code="CCH1221", name="Fine in another dimension").full_clean()


def test_admin_rejects_duplicate_codes_on_one_page(client, org):
    admin_user = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(admin_user)
    form = {"organisation": org[0].pk, "code": "venue", "name": "Venue", "is_active": "on",
            "source_name": "", "attribute_fields": "[]",
            "values-TOTAL_FORMS": "2", "values-INITIAL_FORMS": "0",
            "values-0-code": "ARENA", "values-0-name": "Arena", "values-0-attributes": "{}", "values-0-is_active": "on",
            "values-1-code": "arena", "values-1-name": "Arena again", "values-1-attributes": "{}"}
    response = client.post("/admin/core/dimension/add/", form)
    assert response.status_code == 200
    assert "Code arena is already used by Arena on this page" in response.content.decode()
    assert not Dimension.objects.filter(code="venue").exists()


def test_name_and_file_in_one_command(client, org):
    result = say(client, "/createtag event <<", [csv_file(EVENT_ROWS)])  # bare << is fine: the picker names it
    assert result["subtitle"] == "Step: Review" and "Values (2)" in tables(result)
    assert TagDraft.objects.get().data["name"] == "Event"
    say(client, "approve")
    assert Dimension.objects.get().values.count() == 3


def test_file_before_choosing_is_not_read(client, org):
    result = say(client, "/createTag <<", [csv_file(EVENT_ROWS)])
    assert result["subtitle"] == "Step: New or replace" and "Your file wasn't read" in notes(result)


def test_bare_input_marker_on_a_command_without_files(client):
    data = client.post("/console/run/", {"command": "/status <<"}).json()
    assert not data["ok"] and "doesn't take an input file" in data["error"]
