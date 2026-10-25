"""The accrual engine on a ledger shaped like TECC's 2000: each month's journal worded differently and "based on"
the invoice that arrived after month end, payroll and revenue moved through the same account, and a month whose
own accrual must not feed its expectation."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from accounting.ledger import run_accruals
from accounting.models import Accrual, AccrualProfile, Behaviour, Method, State
from accounting.scan import scan
from accounting.util import add_months, month_end
from core.models import Account, Contact

from .conftest import Ledger

D = Decimal
START, END = date(2025, 10, 1), date(2026, 9, 30)
MONTHS = [add_months(START, i) for i in range(12)]
WORDINGS = ["Accrual for Electricity Charges - C S Energy as per Invoice {n}", "Electricity expense based on Invoice {n}",
            "Accrual for Electricity based on Invoice {n}", "Electricity charges based on Invoice {n}"]


@pytest.fixture
def tecc(db):
    L = Ledger()
    L.cs = Contact.objects.create(organisation=L.org, name="CS Energy", is_supplier=True)
    L.lawyers = Contact.objects.create(organisation=L.org, name="Gross Lawyers", is_supplier=True)
    L.salaries = Account.objects.create(organisation=L.org, code="6345", name="Labour - Salaries Event Staff",
                                        account_class="expense")
    for i, m in enumerate(MONTHS):
        amount = D("20000") + 3000 * (i % 3)  # varies about 10%: variable, not fixed
        number = f"T11{i:05d}"
        lines = [(L.power, amount, WORDINGS[i % 4].format(n=number), []),
                 (L.salaries, D("17000"), "Salary accrual", [])]
        if i == 4:
            lines.append((L.sales, D("-800"), "Reallocate revenue ADF event", []))
            lines.append((L.power, D("500"), "Electricity gross up", []))  # names "Gross": not the lawyers' bill
        L.accrue(m, lines)
        L.bill(L.cs, month_end(m) + timedelta(days=6), L.power, amount, number=number)
    L.accrual = Accrual.objects.create(organisation=L.org, account_codes="2000", history_months=12)
    return L


def profiles(accrual):
    return {p.account.code: p for p in AccrualProfile.objects.filter(accrual=accrual)}


def test_one_profile_per_account_whatever_the_wording(tecc):
    result = scan(tecc.accrual, START, END)
    found = profiles(tecc.accrual)
    assert set(found) == {"6100", "6345"}
    power = found["6100"]
    assert power.supplier == tecc.cs and power.supplier_found_by == "invoice number"
    assert power.months_accrued == 12 and power.behaviour == Behaviour.VARIABLE
    assert power.method == Method.RUN_RATE and power.billing_lag_days == 6
    assert power.amount_model["evidenced_months"] == 12 and power.amount_model["max_lag_days"] == 6
    assert "A bill followed in 12 of 12 months, usually dated 6 days after month end, from CS Energy" in power.memo
    assert "Judgement: variable recurring; accrue the bill for the month when it is in by the close" in power.memo
    assert power.evidence[0]["bills"][0]["found_by"] == "invoice number"
    assert result.summary["revenue_lines"] == 2  # revenue moved through 2000 (and back) isn't a cost


def test_payroll_is_kept_apart(tecc):
    scan(tecc.accrual, START, END)
    salaries = profiles(tecc.accrual)["6345"]
    assert salaries.behaviour == Behaviour.PAYROLL and salaries.method == Method.NONE
    assert "Payroll, kept apart from supplier accruals" in salaries.memo
    journal = run_accruals(tecc.accrual, date(2026, 9, 30))
    assert {s["why"] for s in journal.skipped} >= {"payroll, worked out separately (not built yet)"}


def test_the_month_being_accrued_never_feeds_its_own_expectation(tecc):
    from core.models import Document, JournalLine

    JournalLine.objects.filter(account=tecc.power, journal__date=date(2026, 9, 30), amount__gt=0).update(
        amount=D("99999"))  # whatever was posted for September in Xero
    Document.objects.filter(number="T1100011").delete()  # September's bill isn't in yet
    scan(tecc.accrual, START, END)
    journal = run_accruals(tecc.accrual, date(2026, 9, 30))
    line = journal.lines.get(account=tecc.power)
    assert line.state == State.PREDICTED
    assert line.basis == "average of Jun 2026 to Aug 2026" and line.expected_amount == D("23000.00")


def test_the_bill_in_by_the_close_is_the_accrual(tecc):
    scan(tecc.accrual, START, END)
    journal = run_accruals(tecc.accrual, date(2026, 9, 30))
    line = journal.lines.get(account=tecc.power)
    assert line.state == State.EVIDENCED and line.expected_amount == D("26000")  # bill T1100011, 6 Oct
    assert line.basis == "bill T1100011 (CS Energy, 06 Oct)" and line.evidence[0]["number"] == "T1100011"


def test_a_bill_later_than_the_usual_lag_is_not_this_months(tecc):
    from core.models import Document

    Document.objects.filter(number="T1100011").update(date=date(2026, 10, 30))  # 30 days: lag is 6 (+7)
    scan(tecc.accrual, START, END)
    line = run_accruals(tecc.accrual, date(2026, 9, 30)).lines.get(account=tecc.power)
    assert line.state == State.PREDICTED


def test_old_profiles_grouped_by_wording_are_replaced(tecc):
    AccrualProfile.objects.create(organisation=tecc.org, accrual=tecc.accrual, key="6100|d:electricity based|",
                                  name="Electricity Based", behaviour="one_off", detected_behaviour="one_off",
                                  account=tecc.power)
    kept = AccrualProfile.objects.create(organisation=tecc.org, accrual=tecc.accrual, key=f"6100|s{tecc.cs.pk}|",
                                         name="CS Energy", behaviour="one_off", detected_behaviour="one_off",
                                         account=tecc.power, notes="Checked with the venue manager")
    result = scan(tecc.accrual, START, END)
    keys = set(AccrualProfile.objects.filter(accrual=tecc.accrual).values_list("key", flat=True))
    assert "6100|d:electricity based|" not in keys and kept.key in keys  # a reviewed one stays, not recent
    assert result.summary["old_profiles_replaced"] == 1
    tecc.accrual.refresh_from_db()
    assert tecc.accrual.excluded_keys == []
