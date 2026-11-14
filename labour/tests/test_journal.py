"""The pay-run journal: lines by account × department × event, months, rounding, the overlay journal (ADJ) and
its bucket, the checks, the import file and the comparison with the payroll journal posted in Xero."""

import csv
import io
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model

from core.models import Account, Adjustment, Journal, JournalLine, TrackingCategory, TrackingOption
from labour import journal, ledger
from labour.models import LabourOverride, PayComponent, PayRun

from .test_allocation import labour  # noqa: F401  (fixture)
from .test_labour import org, row, workbook  # noqa: F401  (fixture)


@pytest.fixture
def linked(labour):
    """The fixture's events linked to Xero's Event tracking options."""
    org, events = labour
    category = TrackingCategory.objects.create(organisation=org[0], name="Event", source_system="xero",
                                               source_id="tc-event")
    settings = ledger.settings_for(org[0])
    settings.event_dimension.tracking_category = category
    settings.event_dimension.save()
    settings.narration = "Ville for TECC Payroll Journal FNE: {period_end}"
    settings.save()
    for value in events.values():
        value.tracking_option = TrackingOption.objects.create(organisation=org[0], category=category,
                                                              name=value.code, source_system="xero",
                                                              source_id=f"to-{value.code}")
        value.save()
    return org, events


def lines_of(adj):
    return [(l.account_code, l.tracking_option1, l.tracking_option2, l.amount) for l in adj.lines.all()]


def test_journal_by_account_department_event(linked):
    org, events = linked
    run = PayRun.objects.get()
    p, adjustments = journal.build(run)
    assert p.problems == [] and p.target == D("852.60") and p.total() == D("852.60")
    [adj] = adjustments
    assert (adj.reference, adj.date, adj.status, adj.narration, adj.origin) == (
        "ADJ-0001", date(2026, 10, 18), Adjustment.Status.EXPECTED,
        "Ville for TECC Payroll Journal FNE: 18/10/2026", "labour")
    fb, ov = "Food and Beverage Services", "Overhead & Venue Costs"
    # Ann: 10 Oct Gala (300 + 36 super); 11 Oct bump day of Gala and Expo, split (150 + 18 each). Bo: Unassigned.
    assert sorted(lines_of(adj)) == sorted([
        ("6335", fb, "261010GAL", D("12.60")), ("6335", fb, "261013EXP", D("4.20")), ("6335", ov, "", D("3.50")),
        ("6345", fb, "261010GAL", D("504.00")), ("6345", fb, "261013EXP", D("168.00")), ("6345", ov, "", D("140.00")),
        ("6380", fb, "261010GAL", D("12.60")), ("6380", fb, "261013EXP", D("4.20")), ("6380", ov, "", D("3.50")),
        ("2350", "", "", D("-852.60"))])
    first = adj.lines.first()
    assert (first.tracking_name1, first.tracking_name2) == ("Departments", "Event")

    rows = list(csv.reader(io.StringIO(journal.import_csv(run, adjustments).decode())))
    assert rows[0] == journal.COLUMNS and len(rows) == 11
    assert rows[1][:2] == ["Ville for TECC Payroll Journal FNE: 18/10/2026", "18/10/2026"]
    assert sum(D(r[5]) for r in rows[1:]) == 0

    # Rebuilding keeps the number; a labour change rebuilds it.
    LabourOverride.objects.create(organisation=org[0], date_from=date(2026, 10, 11), date_to=date(2026, 10, 11),
                                  value=events["261013EXP"])
    _, [again] = journal.build(run)
    assert again.pk == adj.pk and ("6345", fb, "261013EXP", D("336.00")) in lines_of(again)


def test_pay_run_across_months_two_journals_rounded(org):
    rows = [row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Normal Pay", "Hours By Rate",
                datetime(2026, 9, 29), datetime(2026, 10, 1), 3, 100.00, period_end=datetime(2026, 10, 4))]
    ledger.upload(org[0], org[1], "b.xlsx", workbook(rows=rows, summary_total=D("100.00")))
    PayComponent.objects.update(gl_account="6345")
    p, adjustments = journal.build(PayRun.objects.get())
    assert [(a.date, a.period_end) for a in adjustments] == [(date(2026, 9, 30), date(2026, 9, 30)),
                                                             (date(2026, 10, 4), date(2026, 10, 31))]
    debits = [a.debit_total for a in adjustments]
    assert sum(debits) == p.target and debits[0] in (D("66.67"), D("66.66"))  # 2 of 3 days in September
    assert any("department" in t for t in p.problems)  # the cost account has no department yet: no download


def test_checks_block_the_file(labour):
    org, _ = labour
    run = PayRun.objects.get()
    p, _ = journal.build(run)
    assert any("isn't linked to a Xero tracking category" in t for t in p.problems)
    assert any("261010GAL" in t for t in p.problems)


def test_superseded_by_the_posted_journal_and_the_view(client, linked):
    org, _ = linked
    run = PayRun.objects.get()
    posted = Journal.objects.create(organisation=org[0], date=date(2026, 10, 18), status=Journal.Status.POSTED,
                                    narration="Ville for TECC Payroll Journal FNE: 18/10/2026", source_system="xero",
                                    source_id="j-1")
    accounts = {a.code: a for a in Account.objects.filter(organisation=org[0])}
    overhead = TrackingOption.objects.get(name="Overhead & Venue Costs")
    for n, (code, amount) in enumerate((("6345", "812.00"), ("2350", "-812.00")), 1):
        line = JournalLine.objects.create(organisation=org[0], journal=posted, line_number=n, account=accounts[code],
                                          amount=D(amount))
        if code == "6345":
            line.tracking.add(overhead)
    p, adjustments = journal.build(run)
    assert {a.status for a in adjustments} == {Adjustment.Status.SUPERSEDED}
    assert adjustments[0].superseded_by == posted
    diff = {(r[0][:4], r[1]): r[4] for r in journal.xero_difference(run, adjustments)}
    assert diff[("6345", "Food and Beverage Services")] == D("672.00")
    assert diff[("6345", "Overhead & Venue Costs")] == D("-672.00")
    assert diff[("6335", "Overhead & Venue Costs")] == D("3.50")

    boss = get_user_model().objects.create_superuser("boss", password="pw")
    client.force_login(boss)
    base = f"/admin/labour/labourview/?organisation={org[0].pk}&month=2026-10&tab=journal"
    page = client.get(base).content.decode()
    assert "ADJ-0001" in page and "Superseded" in page and "852.60" in page and "ties to the other tabs" in page
    response = client.get(f"{base}&download={run.pk}")
    assert response.status_code == 200 and response["Content-Type"] == "text/csv"
    assert b"Payroll Journal FNE: 18/10/2026" in response.content
    assert client.get("/admin/core/adjustment/").status_code == 200
