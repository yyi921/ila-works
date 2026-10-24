"""The accrual scan on the TECC-like ledger in conftest, and changing what it learned in admin."""

from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from accounting.models import AccrualProfile, Behaviour, Method
from accounting.scan import description_key, scan
from core.models import Account, Organisation

from .conftest import END, START, by_account

D = Decimal


@pytest.fixture
def admin_client(client, db):
    client.force_login(get_user_model().objects.create_superuser("admin", password="pw"))
    return client


def test_description_key_drops_months_years_and_accrual_words():
    assert description_key("Accrue Origin electricity Sep-26") == "origin electricity"
    assert description_key("Reversal: Origin Electricity accrual October 2026") == "origin electricity"


def test_scan_sorts_each_cost_into_its_behaviour(ledger):
    result = scan(ledger.accrual, START, END)
    found = {code: p.behaviour for code, p in by_account(result).items()}
    assert found == {"6100": Behaviour.VARIABLE, "6200": Behaviour.FIXED, "6600": Behaviour.FIXED,
                     "6300": Behaviour.DRIVER, "6400": Behaviour.PERIODIC, "6500": Behaviour.ONE_OFF}
    assert result.summary["balance_sheet_lines"] == 1  # the prepayment reclass is not a cost


def test_variable_cost_learns_supplier_lag_accuracy_and_seasonality(ledger):
    power = by_account(scan(ledger.accrual, START, END))["6100"]
    assert power.supplier == ledger.origin and power.supplier_found_by == "description"
    assert list(power.tracking.all()) == [ledger.hall]
    assert power.months_accrued == 24 and power.is_current
    assert power.billing_lag_days == 12
    assert power.accuracy_mape == pytest.approx(0.03 / 1.03, abs=0.001)
    assert power.seasonality["Jan"] > 1.15
    assert "Reversed on day 1 of the next month in 24 of 24 months" in power.memo
    assert "higher in Jan, Feb, Dec" in power.memo
    assert power.method == Method.RUN_RATE and power.amount == D("15000.00")
    assert power.rule_text.startswith("Accrue the run-rate ($15,000) × the month's seasonal index, Dr 6100 "
                                      "(Main Hall)")


def test_supplier_found_from_bills_when_the_journal_does_not_name_it(ledger):
    cleaning = by_account(scan(ledger.accrual, START, END))["6600"]
    assert cleaning.supplier == ledger.sparkle and cleaning.supplier_found_by == "bills"
    assert (cleaning.method, cleaning.amount, cleaning.billing_lag_days) == (Method.FIXED, D("5000.00"), 5)


def test_fixed_cost_without_bills_says_so(ledger):
    software = by_account(scan(ledger.accrual, START, END))["6200"]
    assert software.average == D("1200.00")
    assert "No bill within 35%" in software.memo
    assert software.rule_text.startswith("Accrue $1,200 every month")


def test_driver_cost_follows_events(ledger):
    security = by_account(scan(ledger.accrual, START, END))["6300"]
    assert security.driver == "Event" and security.driver_category.name == "Events Department"
    assert security.driver_rate == D("2000.00")
    assert not security.tracking.exists()  # events are the driver, not part of the profile key
    assert security.method == Method.DRIVER_RATE
    assert "$2,000 per event in the month" in security.rule_text
    assert "Reversed on day 1 of the next month in 24 of 24 months" in security.memo


def test_periodic_and_one_off(ledger):
    found = by_account(scan(ledger.accrual, START, END))
    rates, repairs = found["6400"], found["6500"]
    assert rates.supplier is None  # "Council rates" names no supplier
    assert rates.method == Method.PREPAYMENT and rates.amount_model["median_gap_months"] == 3
    assert "next expected Dec 2026" in rates.memo and "prepayments or amortisation" in rates.memo
    assert repairs.method == Method.EVIDENCE_ONLY and repairs.confidence <= 0.3


def test_thresholds_on_the_set_up_change_the_result(ledger):
    ledger.accrual.fixed_variation = D("15")  # electricity varies 12%: now counts as fixed
    ledger.accrual.bill_tolerance = D("1")    # only near-exact bills match: the 3% electricity gap no longer does
    ledger.accrual.save()
    power = by_account(scan(ledger.accrual, START, END))["6100"]
    assert power.behaviour == Behaviour.FIXED
    assert power.billing_lag_days is None and "No bill within 1%" in power.memo


def test_rescan_keeps_what_a_person_changed(ledger):
    scan(ledger.accrual, START, END)
    security = AccrualProfile.objects.get(account=ledger.security)
    security.driver, security.driver_rate, security.memo = "", None, "Security is now a flat monthly contract."
    security.notes = "Changed supplier in Aug"
    security.keep(["driver", "driver_rate", "memo"])
    security.save()
    software = AccrualProfile.objects.get(account=ledger.software)
    software.behaviour = Behaviour.PERIODIC
    software.keep(["behaviour"])  # a new behaviour brings its method unless the method was changed too
    software.save()

    scan(ledger.accrual, START, END)
    security.refresh_from_db()
    software.refresh_from_db()
    assert (security.driver, security.driver_rate) == ("", None)
    assert security.memo == "Security is now a flat monthly contract." and security.notes
    assert security.rule_text.startswith("No driver or rate set: accrue the run-rate")
    assert security.months_accrued == 24  # statistics are still refreshed
    assert software.behaviour == Behaviour.PERIODIC and software.detected_behaviour == Behaviour.FIXED
    assert software.method == Method.PREPAYMENT


def test_deleted_profiles_stay_deleted_and_old_ones_are_kept(ledger):
    scan(ledger.accrual, START, END)
    AccrualProfile.objects.get(account=ledger.software).delete()
    result = scan(ledger.accrual, START, END)
    assert "6200" not in by_account(result) and result.summary["deleted_by_you"] == 1
    ledger.accrual.refresh_from_db()
    assert len(ledger.accrual.excluded_keys) == 1

    scan(ledger.accrual, date(2026, 4, 1), date(2026, 9, 30))  # repairs (Mar 2026) is outside
    repairs = AccrualProfile.objects.get(account=ledger.repairs)
    assert not repairs.is_current  # kept, marked not recent

    ledger.accrual.excluded_keys = []
    ledger.accrual.save()
    assert "6200" in by_account(scan(ledger.accrual, START, END))


def test_missing_accrual_account_is_explained(ledger):
    ledger.accrual.account_codes = "2999"
    with pytest.raises(ValueError, match="No account with code 2999"):
        scan(ledger.accrual, START, END)


def test_admin_shows_accounting_sections(ledger, admin_client):
    index = admin_client.get(reverse("admin:index")).content.decode()
    for name in ("Accounting", "Accruals", "Accrual profiles", "Expectation Ledger", "Prepayments", "COGS"):
        assert name in index

    response = admin_client.post(reverse("admin:accounting_accrual_changelist"),
                                 {"action": "scan_history", "_selected_action": [ledger.accrual.pk]}, follow=True)
    assert "profiles learned" in response.content.decode()

    page = admin_client.get(reverse("admin:accounting_accrual_change", args=[ledger.accrual.pk])).content.decode()
    for heading in ("Fixed recurring", "Variable recurring", "Driver-linked", "Periodic, non-monthly", "One-off",
                    "Thresholds", "Fixed if amounts vary less than (%)"):
        assert heading in page
    assert "Origin Energy Pty Ltd" in page and "run-rate × seasonality" in page
    profile = AccrualProfile.objects.get(account=ledger.power)
    assert admin_client.get(reverse("admin:accounting_accrualprofile_change", args=[profile.pk])).status_code == 200


def test_admin_edits_are_kept_and_deletes_remembered(ledger, admin_client):
    scan(ledger.accrual, START, END)
    profile = AccrualProfile.objects.get(account=ledger.security)
    url = reverse("admin:accounting_accrualprofile_change", args=[profile.pk])
    form = admin_client.get(url).context["adminform"].form
    other = Organisation.objects.create(name="C&M")
    Account.objects.create(organisation=other, code="6100", name="Electricity", account_class="expense")
    assert set(form.fields["account"].queryset.values_list("organisation", flat=True)) == {ledger.org.pk}
    data = {k: v for k, v in form.initial.items() if v is not None and k in form.fields}
    data |= {"tracking": [], "seasonality": "{}", "kept_fields": "[]", "driver": "", "driver_rate": "",
             "amount": "6000", "driver_category": "", "supplier": "", "notes": ""}
    response = admin_client.post(url, data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    profile.refresh_from_db()
    assert set(profile.kept_fields) >= {"driver", "driver_rate", "amount"}
    assert profile.rule_text.startswith("No driver or rate set: accrue the run-rate ($6,000)")

    delete = reverse("admin:accounting_accrualprofile_delete", args=[profile.pk])
    admin_client.post(delete, {"post": "yes"})
    ledger.accrual.refresh_from_db()
    assert profile.key in ledger.accrual.excluded_keys


def test_management_command_prints_memos_and_rules(ledger):
    out = StringIO()
    call_command("scan_accruals", "TECC", "--end", "2026-09-30", stdout=out)
    text = out.getvalue()
    assert "TECC: 6 profiles, Oct 2024 to Sep 2026" in text
    assert "[Variable recurring] Origin Energy Pty Ltd — 6100 Electricity (Main Hall)" in text
    assert "Rule: Accrue $1,200 every month" in text
