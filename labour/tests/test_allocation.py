"""Labour by day and by event: the spread over days, the allocation rules, people's changes, and the Labour view."""

from datetime import date, datetime
from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model

from core.models import DimensionValue
from labour import allocation, ledger
from labour.models import Employee, LabourOverride, OnCost, PayComponent

from .test_labour import org, row, workbook  # noqa: F401  (fixture)

# Ann (F&B, taggable): 10 hours on each of 10 and 11 Oct ($300 a day), super over the fortnight.
# Bo (Fixed Wages, not taggable): $140 over 5-18 Oct ($10 a day).
ROWS = [
    row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Normal Pay", "Hours By Rate",
        datetime(2026, 10, 10), datetime(2026, 10, 11), 20, 600.00),
    row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Superannuation", "Super",
        datetime(2026, 10, 5), datetime(2026, 10, 18), 0, 72.00),
    row(222, "Bo Chen", "700-2100", "TECC - Fixed Wages", "Normal Pay", "Auto Pay",
        datetime(2026, 10, 5), datetime(2026, 10, 18), 76, 140.00),
]


@pytest.fixture
def labour(org):
    from labour.models import CostAccount  # noqa: PLC0415

    ledger.upload(org[0], org[1], "a.xlsx", workbook(rows=ROWS, summary_total=D("812.00")))
    CostAccount.objects.filter(code="700-2100").update(taggable=False, department="Overhead & Venue Costs")
    CostAccount.objects.filter(code="700-3000").update(department="Food and Beverage Services")
    PayComponent.objects.update(gl_account="6345")
    settings = ledger.settings_for(org[0])
    OnCost.objects.filter(settings=settings).update(rate=D("0.025"))  # 2.5% each, 5% in all
    dim = settings.event_dimension
    events = {code: DimensionValue.objects.create(organisation=org[0], dimension=dim, code=code, name=name,
                                                  start_date=start, end_date=end)
              for code, name, start, end in [("261010GAL", "Gala", date(2026, 10, 10), None),
                                             ("261013EXP", "Expo", date(2026, 10, 13), None),
                                             ("2627FIRE", "Fire season", date(2026, 10, 1), date(2027, 2, 28))]}
    return org, events


def oct_(org):
    return allocation.month(org[0], date(2026, 10, 1), date(2026, 10, 31))


def pieces_on(m, employee_code, day):
    employee = Employee.objects.get(code=employee_code)
    return [p for p in m.pieces if p.day == day and p.employee_id == employee.pk]


def test_spread_super_follows_wages(labour):
    org, _ = labour
    m = oct_(org)
    assert sum(p.amount for p in m.pieces) == D("812.00")
    ann_10 = pieces_on(m, "111", date(2026, 10, 10))
    assert sorted((p.component.code, p.amount, p.hours) for p in ann_10) == [
        ("Normal", D("300"), D("10")), ("Super", D("36"), D("0"))]  # super only on the days she worked
    assert not pieces_on(m, "111", date(2026, 10, 12))
    bo = pieces_on(m, "222", date(2026, 10, 12))
    assert [(p.amount, p.rule, p.reason) for p in bo] == [(D("10"), "not_taggable", "Not taggable (TECC - Fixed Wages)")]


def test_rules_event_beats_bump_beats_season(labour):
    org, events = labour
    m = oct_(org)
    day10 = pieces_on(m, "111", date(2026, 10, 10))[0]  # Gala's day; Fire season too; Expo is 3 days off
    assert (day10.rule, day10.reason, day10.shares) == ("event", "Event day: 261010GAL",
                                                         [(events["261010GAL"].pk, D("1"))])
    day11 = pieces_on(m, "111", date(2026, 10, 11))[0]  # Gala +1 and Expo -2: both bump days, split
    assert day11.rule == "bump" and day11.reason == "Bump-out +1: 261010GAL; Bump-in -2: 261013EXP (split equally)"
    assert sorted(v for v, _ in day11.shares) == sorted([events["261010GAL"].pk, events["261013EXP"].pk])
    events["261013EXP"].bump_in_days = 0
    events["261013EXP"].save()
    events["261010GAL"].bump_out_days = 0
    events["261010GAL"].save()
    day11 = pieces_on(oct_(org), "111", date(2026, 10, 11))[0]  # no bump days now: only the season
    assert (day11.rule, day11.reason) == ("season", "Season: 2627FIRE")
    events["2627FIRE"].is_active = False
    events["2627FIRE"].save()
    day11 = pieces_on(oct_(org), "111", date(2026, 10, 11))[0]
    assert (day11.rule, day11.reason, day11.shares) == ("none", "No event", [(None, D("1"))])


def test_changes_by_employee_beat_changes_by_day_beat_the_calendar(labour):
    org, events = labour
    ann = Employee.objects.get(code="111")
    LabourOverride.objects.create(organisation=org[0], date_from=date(2026, 10, 10), date_to=date(2026, 10, 11),
                                  value=events["261013EXP"], note="Expo set-up")
    m = oct_(org)
    assert pieces_on(m, "111", date(2026, 10, 10))[0].reason == "Day set by you: Expo set-up"
    LabourOverride.objects.create(organisation=org[0], employee=ann, date_from=date(2026, 10, 11),
                                  date_to=date(2026, 10, 11), value=None)
    m = oct_(org)
    assert pieces_on(m, "111", date(2026, 10, 11))[0].shares == [(None, D("1"))]
    assert pieces_on(m, "111", date(2026, 10, 11))[0].rule == "employee"
    assert pieces_on(m, "222", date(2026, 10, 11))[0].rule == "not_taggable"  # changes never move Fixed Wages

    totals = allocation.by_event(m)
    expo = totals[events["261013EXP"].pk]
    assert (expo["wages"], expo["super"], expo["on_costs"], expo["total"]) == (D("300"), D("36"), D("16.80"),
                                                                              D("352.80"))
    unassigned = totals[None]
    assert unassigned["not_taggable"] == D("147.00") and unassigned["no_event"] == D("352.80")
    assert sum(r["total"] for r in totals.values()) == D("852.60")  # 812 + 5%
    by_day = allocation.by_day(m)
    assert by_day[date(2026, 10, 10)]["values"][events["261013EXP"].pk] == D("352.80")
    assert sum(allocation.by_employee(m, with_on_costs=False)[ann.pk].values()) == D("672")


def test_labour_view_tabs_and_changes(client, labour):
    org, events = labour
    boss = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(boss)
    base = f"/admin/labour/labourview/?organisation={org[0].pk}&month=2026-10"
    for tab in ("employee", "day", "event"):
        page = client.get(f"{base}&tab={tab}").content.decode()
        assert "852.60" in page, tab  # every tab ties to the same total
    page = client.get(f"{base}&tab=event").content.decode()
    assert "261010GAL · Gala" in page and "not taggable 147.00" in page

    ann = Employee.objects.get(code="111")
    page = client.get(f"{base}&employee={ann.pk}").content.decode()
    assert "Event day: 261010GAL" in page and "Change where Ann Lee" in page
    response = client.post("/admin/labour/labourview/", {
        "organisation": org[0].pk, "month": "2026-10", "employee": ann.pk, "date_from": "2026-10-10",
        "date_to": "2026-10-11", "value": events["261013EXP"].pk, "note": "Worked the Expo"})
    assert response.status_code == 302 and f"employee={ann.pk}" in response["Location"]
    change = LabourOverride.objects.get()
    assert (change.employee, change.value, change.set_by) == (ann, events["261013EXP"], boss)
    page = client.get(f"{base}&employee={ann.pk}").content.decode()
    assert "Changed by you: Worked the Expo" in page
    bad = client.post("/admin/labour/labourview/", {
        "organisation": org[0].pk, "month": "2026-10", "date_from": "2026-10-12", "date_to": "2026-10-11",
        "value": ""}, follow=True).content.decode()
    assert "To can&#x27;t be before From" in bad and LabourOverride.objects.count() == 1
    client.post("/admin/labour/labourview/", {"organisation": org[0].pk, "month": "2026-10", "remove": change.pk})
    assert not LabourOverride.objects.exists()
    assert "Labour view" in client.get("/admin/").content.decode()
