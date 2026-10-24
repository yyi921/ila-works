"""A small TECC-like ledger: 24 months (Oct 2024 – Sep 2026) of accrual journals to 2000,
reversed on the 1st, and the bills that followed."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from accounting.models import Accrual
from accounting.util import add_months, month_end
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
        self.sales = acct("4000", "Venue Hire", "revenue")
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

    def bill(self, contact, day, account, amount, number=""):
        doc = Document.objects.create(organisation=self.org, type="bill", ledger="payable", status="paid",
                                      contact=contact, date=day, line_amount_type="exclusive", total=amount,
                                      number=number)
        DocumentLine.objects.create(organisation=self.org, document=doc, account=account, line_amount=amount)
        return doc

    def invoice(self, day, events):
        """A sales invoice with a line per event: how a run counts events held in the month."""
        doc = Document.objects.create(organisation=self.org, type="invoice", ledger="receivable", status="paid",
                                      date=day, total=D("100") * len(events))
        for event in events:
            line = DocumentLine.objects.create(organisation=self.org, document=doc, account=self.sales,
                                               line_amount=D("100"))
            line.tracking.set([event])


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


@pytest.fixture(autouse=True)
def jobs_run_straight_away(monkeypatch):
    """Admin jobs run in a background thread in production; in tests they run before the action returns."""
    monkeypatch.setattr("accounting.jobs.start", lambda work: work())
