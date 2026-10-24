"""The accrual scan on a small TECC-like ledger: 24 months (Oct 2024 – Sep 2026) of accrual journals to 2000,
reversed on the 1st, and the bills that followed."""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from accounting.models import Accrual, AccrualProfile, Behaviour
from accounting.scan import add_months, description_key, month_end, scan
from core.models import (Account, Contact, Document, DocumentLine, Journal, JournalLine, Organisation,
                         TrackingCategory, TrackingOption)

D = Decimal
START, END = date(2024, 10, 1), date(2026, 9, 30)
MONTHS = [add_months(START, i) for i in range(24)]


class Ledger:
    def __init__(self):
        self.org = Organisation.objects.create(name="TECC")
        acct = lambda code, name, cls: Account.objects.create(organisation=self.org, code=code, name=name,
                                                              account_class=cls)
        self.accruals = acct("2000", "Accrued Expenses", "liability")
        self.prepay = acct("1400", "Prepayments", "asset")
        self.power = acct("6100", "Electricity", "expense")
        self.software = acct("6200", "Software", "expense")
        self.security = acct("6300", "Security", "expense")
        self.rates = acct("6400", "Rates", "expense")
        self.repairs = acct("6500", "Repairs", "expense")
        self.cleaning = acct("6600", "Cleaning", "expense")
        supplier = lambda name: Contact.objects.create(organisation=self.org, name=name, is_supplier=True)
        self.origin = supplier("Origin Energy Pty Ltd")
        self.sparkle = supplier("Sparkle Cleaners Pty Ltd")
        self.council = supplier("Brisbane City Council")
        events = TrackingCategory.objects.create(organisation=self.org, name="Events Department")
        self.events = [TrackingOption.objects.create(organisation=self.org, category=events, name=f"EV{i}")
                       for i in range(12)]
        venue = TrackingCategory.objects.create(organisation=self.org, name="Venue")
        self.hall = TrackingOption.objects.create(organisation=self.org, category=venue, name="Main Hall")
        self.n = 0

    def journal(self, day, lines, narration=""):
        self.n += 1
        j = Journal.objects.create(organisation=self.org, date=day, narration=narration, status="posted",
                                   number=f"MJ{self.n}")
        total = D("0")
        for i, (account, amount, description, tracking) in enumerate(lines, 1):
            line = JournalLine.objects.create(organisation=self.org, journal=j, line_number=i, account=account,
                                              amount=amount, description=description)
            line.tracking.set(tracking)
            total += amount
        if total:
            JournalLine.objects.create(organisation=self.org, journal=j, line_number=99, account=self.accruals,
                                       amount=-total)
        return j

    def accrue(self, month, lines):
        """Accrue at month end and reverse on the 1st of the next month, as Xero's auto-reversal does."""
        self.journal(month_end(month), lines)
        self.journal(add_months(month, 1), [(a, -amt, f"Reversal: {d}", t) for a, amt, d, t in lines])

    def bill(self, contact, day, account, amount):
        doc = Document.objects.create(organisation=self.org, type="bill", ledger="payable", status="paid",
                                      contact=contact, date=day, line_amount_type="exclusive", total=amount)
        DocumentLine.objects.create(organisation=self.org, document=doc, account=account, line_amount=amount)


def power_amount(month):
    return D("15000") * (D("1.3") if month.month in (12, 1, 2) else D("1"))


@pytest.fixture
def ledger(db):
    L = Ledger()
    for i, m in enumerate(MONTHS):
        label = m.strftime("%b-%y")
        k = 2 + i % 2  # two or three events a month
        lines = [(L.power, power_amount(m), f"Accrue Origin electricity {label}", [L.hall]),
                 (L.software, D("1200"), f"Microsoft 365 subscription accrual {m:%B %Y}", []),
                 (L.cleaning, D("5000"), f"Contract cleaning accrual {label}", [])]
        lines += [(L.security, D("2000"), f"Security accrual {label}", [L.events[(i + e) % 12]]) for e in range(k)]
        if i % 3 == 2:
            lines.append((L.rates, D("9000"), f"Council rates accrual Q{i // 3}", []))
        L.accrue(m, lines)
        L.bill(L.origin, month_end(m) + timedelta(days=12), L.power, power_amount(m) * D("1.03"))
        L.bill(L.sparkle, month_end(m) + timedelta(days=5), L.cleaning, D("5000"))
    L.accrue(date(2026, 3, 1), [(L.repairs, D("4000"), "Repair to kitchen exhaust fan", [])])
    L.journal(date(2026, 6, 30), [(L.prepay, D("700"), "Reclass to prepayments", [])])
    L.accrual = Accrual.objects.create(organisation=L.org, account_codes="2000")
    return L


def by_account(result):
    return {p.account.code: p for p in result.profiles}


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
    assert power.amount_model["seasonality"]["Jan"] > 1.15
    assert "Reversed on day 1 of the next month in 24 of 24 months" in power.memo
    assert "higher in Jan, Feb, Dec" in power.memo
    assert power.rule["method"] == "run_rate" and power.rule["expected_bill_lag_days"] == 12
    assert power.rule_text.startswith("Accrue the last three months' average")


def test_supplier_found_from_bills_when_the_journal_does_not_name_it(ledger):
    cleaning = by_account(scan(ledger.accrual, START, END))["6600"]
    assert cleaning.supplier == ledger.sparkle and cleaning.supplier_found_by == "bills"
    assert cleaning.rule == cleaning.rule | {"method": "fixed", "amount": 5000.0, "expected_bill_lag_days": 5}


def test_fixed_cost_without_bills_says_so(ledger):
    software = by_account(scan(ledger.accrual, START, END))["6200"]
    assert software.average == D("1200.00")
    assert "No bill within 35%" in software.memo
    assert software.rule_text.startswith("Accrue $1,200 every month")


def test_driver_cost_follows_events(ledger):
    security = by_account(scan(ledger.accrual, START, END))["6300"]
    assert security.driver == "Event"
    assert security.amount_model["driver_rate"] == 2000
    assert not security.tracking.exists()  # events are the driver, not part of the profile key
    assert security.rule["method"] == "driver_rate"
    assert "$2,000 per event held in the month" in security.rule_text
    assert "Reversed on day 1 of the next month in 24 of 24 months" in security.memo


def test_periodic_and_one_off(ledger):
    found = by_account(scan(ledger.accrual, START, END))
    rates, repairs = found["6400"], found["6500"]
    assert rates.supplier is None and rates.rule["cadence_months"] == 3  # "Council rates" names no supplier
    assert rates.rule["next_expected"] == "2026-12"
    assert "prepayments or amortisation" in rates.memo
    assert repairs.rule["method"] == "evidence_only" and repairs.confidence <= 0.3


def test_rescan_keeps_reviewer_choices_and_drops_untouched_stale_profiles(ledger):
    scan(ledger.accrual, START, END)
    software = AccrualProfile.objects.get(account=ledger.software)
    software.behaviour, software.notes = Behaviour.PERIODIC, "Billed annually in arrears"
    software.save()
    scan(ledger.accrual, START, END)
    software.refresh_from_db()
    assert software.behaviour == Behaviour.PERIODIC and software.detected_behaviour == Behaviour.FIXED
    assert software.notes == "Billed annually in arrears"

    later = scan(ledger.accrual, date(2026, 4, 1), date(2026, 9, 30))  # repairs (Mar 2026) is outside
    assert not AccrualProfile.objects.filter(account=ledger.repairs).exists()
    assert len(later.profiles) == 5


def test_missing_accrual_account_is_explained(ledger):
    ledger.accrual.account_codes = "2999"
    with pytest.raises(ValueError, match="No account with code 2999"):
        scan(ledger.accrual, START, END)


def test_admin_shows_accounting_sections(ledger, client):
    admin = get_user_model().objects.create_superuser("admin", password="pw")
    client.force_login(admin)
    index = client.get(reverse("admin:index")).content.decode()
    assert "Accounting" in index and "Accruals" in index and "Prepayments" in index and "COGS" in index
    assert "Accrual profiles" not in index

    response = client.post(reverse("admin:accounting_accrual_changelist"),
                           {"action": "scan_history", "_selected_action": [ledger.accrual.pk]}, follow=True)
    assert "profiles learned" in response.content.decode()

    page = client.get(reverse("admin:accounting_accrual_change", args=[ledger.accrual.pk])).content.decode()
    for heading in ("Fixed recurring", "Variable recurring", "Driver-linked", "Periodic, non-monthly", "One-off"):
        assert heading in page
    assert "Origin Energy Pty Ltd" in page and "run-rate × seasonality" in page
    profile = AccrualProfile.objects.get(account=ledger.power)
    assert client.get(reverse("admin:accounting_accrualprofile_change", args=[profile.pk])).status_code == 200


def test_management_command_prints_memos_and_rules(ledger):
    out = StringIO()
    call_command("scan_accruals", "TECC", "--end", "2026-09-30", stdout=out)
    text = out.getvalue()
    assert "TECC: 6 profiles, Oct 2024 to Sep 2026" in text
    assert "[Variable recurring] Origin Energy Pty Ltd — 6100 Electricity (Main Hall)" in text
    assert "Rule: Accrue $1,200 every month" in text

