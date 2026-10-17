import pytest
from django.contrib.auth import get_user_model

from core.models import Dimension, DimensionValue, Organisation, TrackingCategory, TrackingOption
from xero.api import XeroAPIError

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


@pytest.fixture
def dims(org):
    org = org[0]
    category = TrackingCategory.objects.create(organisation=org, name="Event", source_system="xero", source_id="tc1")
    option = TrackingOption.objects.create(organisation=org, category=category, name="Celtic Nights",
                                           source_system="xero", source_id="to1")
    event = Dimension.objects.create(organisation=org, code="event", name="Event", tracking_category=category,
                                     attribute_fields=[{"key": "venue", "label": "Venue", "type": "text"},
                                                       {"key": "ticketed", "label": "Ticketed", "type": "boolean"}])
    DimensionValue.objects.create(organisation=org, dimension=event, code="CN26", name="Celtic Nights",
                                  tracking_option=option, attributes={"venue": "Arena", "ticketed": True})
    DimensionValue.objects.create(organisation=org, dimension=event, code="OLD", name="Old Gala", is_active=False)
    Dimension.objects.create(organisation=org, code="dept", name="Department")
    Dimension.objects.create(organisation=org, code="campaign", name="Campaign", is_active=False)


def run(client, command):
    data = client.post("/console/run/", {"command": command}).json()
    assert data["ok"], data
    return data["result"]


def tables(result):
    return {b["title"]: [r["cells"] for r in b["rows"]] for b in result["blocks"] if b["type"] == "table"}


def test_dim_lists_xero_categories_and_ila_dimensions(client, dims):
    result = run(client, "/dim")
    figures = next(b for b in result["blocks"] if b["type"] == "figures")["items"]
    assert figures[0]["value"] == "1 of 2 active" and figures[0]["detail"].startswith("2 of 4 in all")
    t = tables(result)
    assert t["Xero tracking categories"] == [["Event", "Active", "2", "1", "Event"]]
    assert t["iLA dimensions"] == [["Department", "dept", "0", "", "", "0", "Active"],
                                   ["Event", "event", "1", "Event", "Venue, Ticketed", "0", "Active"]]


def test_dim_archived_and_alias(client, dims):
    t = tables(run(client, "/dimensions --archived"))
    assert ["Region", "Archived", "1", "0", ""] in t["Xero tracking categories"]
    assert [r[0] for r in t["iLA dimensions"]] == ["Campaign", "Department", "Event"]


def test_dim_detail_shows_options_and_values(client, dims):
    result = run(client, "/dim event")
    t = tables(result)
    assert t["Xero tracking category Event (active)"] == [["Celtic Nights", "Active"], ["Gala Dinner", "Active"]]
    assert t["iLA dimension Event (from Xero Event)"] == [["CN26", "Celtic Nights", "Celtic Nights", "Arena", "Yes",
                                                           "Active"]]
    assert "1 archived option(s) hidden" in " ".join(b["text"] for b in result["blocks"] if b["type"] == "note")
    t = tables(run(client, "/dim event --archived"))
    assert len(t["iLA dimension Event (from Xero Event)"]) == 2


def test_dim_unknown_name(client, dims):
    data = client.post("/console/run/", {"command": "/dim nothing"}).json()
    assert 'No tracking category or dimension matches "nothing"' in data["error"]


def test_dim_still_shows_ila_dimensions_without_xero(client, dims, monkeypatch, fake):
    monkeypatch.setattr(type(fake), "tracking_category_list", lambda self: (_ for _ in ()).throw(
        XeroAPIError("TECC isn't connected to an accounting system.")))
    result = run(client, "/dim")
    assert "couldn't be read" in result["blocks"][0]["text"] and "iLA dimensions" in tables(result)
