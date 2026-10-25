"""Accrual runs saved as Expectation Ledger journals: predicted → evidenced → posted → closed."""

import csv
import io
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from accounting.ledger import LedgerError, build_import_file, check_bills, mark_posted, run_accruals
from accounting.models import AccrualProfile, Expectation, LedgerJournal, State
from accounting.scan import scan
from core.models import Contact, ImportFile

from .conftest import END, START

D = Decimal
SEP = date(2026, 9, 30)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_superuser("erin", password="pw")


@pytest.fixture
def learned(ledger):
    scan(ledger.accrual, START, END)
    ledger.invoice(date(2026, 9, 12), ledger.events[:3])  # three events held in September
    return ledger


def lines(journal):
    return {line.account.code: line for line in journal.lines.all()}


def test_run_saves_a_journal_of_expectations(learned, user):
    journal = run_accruals(learned.accrual, SEP, user)
    found = lines(journal)
    assert journal.narration == "Accruals September 2026" and journal.created_by == user

    # Origin's and Sparkle's September bills (12 and 5 Oct) are already in: evidenced, at the bill amount.
    power = found["6100"]
    assert power.state == State.EVIDENCED and power.expected_amount == D("15450.00")
    assert power.evidence[0]["type"] == "bill" and power.confidence == 0.95
    assert [h.get("to") for h in power.history] == ["evidenced"]  # the bill was in by the close: no estimate
    assert found["6600"].state == State.EVIDENCED

    software = found["6200"]
    assert software.state == State.PREDICTED and software.expected_amount == D("1200.00")
    assert software.basis == "the usual amount (Sep 2025 to Aug 2026)"  # not September's own accrual
    assert software.history[0]["by"] == "erin"
    security = found["6300"]
    assert security.expected_amount == D("6000.00") and "3 events on sales invoices" in security.basis

    skipped = {s["profile"].split(" — ")[0]: s["why"] for s in journal.skipped}
    assert skipped["6400 Rates"] == "prepayment, not accrued"
    assert skipped["6500 Repairs"] == "no bill found after month end"
    assert journal.state == State.PREDICTED  # the least advanced line


def test_run_respects_switches_and_materiality(learned, user):
    AccrualProfile.objects.filter(account=learned.cleaning).update(is_active=False)
    learned.accrual.materiality = D("1500")
    learned.accrual.save()
    journal = run_accruals(learned.accrual, SEP, user)
    skipped = {s["profile"].split(" — ")[0]: s["why"] for s in journal.skipped}
    assert skipped["Sparkle Cleaners Pty Ltd"] == "switched off"
    assert "below the $1,500 materiality floor" in skipped["6200 Software"]


def test_bill_dated_in_the_month_is_last_months_for_a_supplier_billing_in_arrears(learned, user):
    # Origin's August bill is dated 12 Sep: still accrue September. A second September bill means it's in.
    assert "6100" in lines(run_accruals(learned.accrual, SEP, user))
    learned.bill(learned.origin, date(2026, 9, 29), learned.power, D("15100"), number="ORG-SEP")
    journal = run_accruals(learned.accrual, SEP, user)
    assert "6100" not in lines(journal)
    assert any("ORG-SEP" in s["why"] and "already posted" in s["why"] for s in journal.skipped)


def test_lifecycle_to_closed_feeds_the_profile(learned, user):
    journal = run_accruals(learned.accrual, SEP, user)
    with pytest.raises(LedgerError, match="Build and stamp"):
        mark_posted(journal, user)

    import_file = build_import_file(journal, user)
    assert import_file.status == ImportFile.Status.STAMPED and import_file.stamp_is_valid(bytes(import_file.content))
    rows = list(csv.reader(io.StringIO(bytes(import_file.content).decode())))
    assert rows[0][:6] == ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount"]
    power = [r for r in rows if r[3] == "6100"]
    assert power[0][:2] == ["Accruals September 2026", "30/09/2026"] and power[0][5] == "15450.00"
    assert power[0][6:8] == ["Venue", "Main Hall"]
    assert power[1][:2] == ["Reversal: Accruals September 2026", "01/10/2026"] and power[1][5] == "-15450.00"
    assert sum(D(r[5]) for r in rows[1:]) == 0  # every journal balances against 2000

    mark_posted(journal, user)
    journal.refresh_from_db()
    assert journal.state == State.POSTED and journal.posted_by == user
    assert ImportFile.objects.get(pk=import_file.pk).status == ImportFile.Status.POSTED

    # The software bill arrives: no supplier on the profile, so it is matched on account and amount.
    microsoft = Contact.objects.create(organisation=learned.org, name="Microsoft", is_supplier=True)
    learned.bill(microsoft, date(2026, 10, 9), learned.software, D("1250"), number="MS-1009")
    assert check_bills(journal) >= 3
    found = lines(journal)
    software, power = found["6200"], found["6100"]
    assert software.state == State.CLOSED and software.actual_amount == D("1250") and software.variance == D("50")
    assert power.state == State.CLOSED and power.variance == D("0")  # the evidence was the actual
    assert [h.get("to") for h in software.history if h.get("to")] == ["posted", "closed"]
    profile = AccrualProfile.objects.get(account=learned.software)
    assert profile.outcomes == [{"period": "2026-09-30", "expected": "1200.00", "actual": "1250.00",
                                 "error": 0.04}]
    assert profile.accuracy_mape == pytest.approx(0.04)
    assert journal.refresh_state() == State.POSTED  # security has no bill yet

    with pytest.raises(LedgerError, match="already posted"):
        run_accruals(learned.accrual, SEP, user)


def test_rerun_keeps_lines_a_person_added_or_changed(learned, user):
    journal = run_accruals(learned.accrual, SEP, user)
    software = lines(journal)["6200"]
    software.expected_amount, software.is_manual = D("1300"), True
    software.save()
    Expectation.objects.create(organisation=learned.org, journal=journal, description="Consultant review",
                               account=learned.repairs, expected_amount=D("2500"), is_manual=True)
    journal = run_accruals(learned.accrual, SEP, user)
    found = lines(journal)
    assert found["6200"].expected_amount == D("1300") and found["6500"].description == "Consultant review"
    assert journal.lines.filter(account=learned.software).count() == 1


def test_admin_runs_edits_and_moves_journals(learned, user, client):
    client.force_login(user)
    response = client.post(reverse("admin:accounting_accrual_changelist"),
                           {"action": "run_last_month", "_selected_action": [learned.accrual.pk]}, follow=True)
    assert "saved with" in response.content.decode()

    add_url = reverse("admin:accounting_ledgerjournal_add")
    form = {"accrual": learned.accrual.pk, "kind": "accrual", "period_end": "2026-09-15", "narration": "",
            "notes": ""}
    LedgerJournal.objects.filter(period_end=SEP).delete()
    assert client.post(add_url, form).status_code == 302
    journal = LedgerJournal.objects.get(period_end=SEP)
    assert journal.lines.count() == 4
    again = client.post(add_url, form)
    assert again.status_code == 200 and "already exists" in again.content.decode()

    url = reverse("admin:accounting_ledgerjournal_change", args=[journal.pk])
    page = client.get(url)
    assert "Left out of this run" in page.content.decode() and "Lines by state" in page.content.decode()
    formset = page.context["inline_admin_formsets"][0].formset
    data = {"narration": journal.narration, "notes": ""}
    for key in ("TOTAL_FORMS", "INITIAL_FORMS", "MIN_NUM_FORMS", "MAX_NUM_FORMS"):
        data[f"{formset.prefix}-{key}"] = formset.management_form[key].value()
    software_index = None
    for i, form in enumerate(formset.forms):
        line = form.instance
        values = {"id": line.pk, "journal": journal.pk, "description": line.description,
                  "account": line.account_id, "expected_amount": str(line.expected_amount),
                  "state": line.state, "why": ""}
        if line.reverse_next_month:
            values["reverse_next_month"] = "on"
        if line.account == learned.software:
            software_index = i
            values |= {"expected_amount": "1350.00", "why": "Price rise from October invoice"}
        if line.account == learned.cleaning:
            values["DELETE"] = "on"
        data |= {f"{formset.prefix}-{i}-{k}": v for k, v in values.items()}
    assert software_index is not None
    response = client.post(url, data)
    assert response.status_code == 302, response.context and response.context["inline_admin_formsets"][0].formset.errors

    software = journal.lines.get(account=learned.software)
    assert software.expected_amount == D("1350.00") and software.is_manual
    assert software.history[-1]["by"] == "erin" and software.history[-1]["why"] == "Price rise from October invoice"
    assert software.history[-1]["expected_amount_before"] == "1200.00"
    assert not journal.lines.filter(account=learned.cleaning).exists()
    journal.refresh_from_db()
    assert "deleted line" in journal.notes

    changelist = reverse("admin:accounting_ledgerjournal_changelist")
    client.post(changelist, {"action": "build_file", "_selected_action": [journal.pk]})
    client.post(changelist, {"action": "posted", "_selected_action": [journal.pk]})
    journal.refresh_from_db()
    assert journal.state == State.POSTED


def test_run_accruals_command(learned):
    out = StringIO()
    call_command("run_accruals", "TECC", "--period", "2026-09", stdout=out)
    text = out.getvalue()
    assert "Accruals September 2026: Predicted, 4 lines" in text
    assert "[Evidenced] Origin Energy Pty Ltd Sep 2026: 15,450.00" in text
    assert "left out: 6400 Rates: prepayment, not accrued" in text
