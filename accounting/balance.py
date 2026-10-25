"""The accrual account as Xero has it, read from iLA's copy of the manual journals: what was accrued in a month, and
every accrual still open at a date. Used to compare the Expectation Ledger's own view with the ledger, never to
form it.

An accrual raises the accrual account (a credit); a reversal, or a bill coded straight to the account, takes it
back (a debit). A reversal is matched to the accrual it reverses by the narration ("Reversal: <narration>") and the
amount, else by the amount alone (the oldest open accrual first). A bill coded to the account clears the accruals
that name its invoice (or a number it starts with), else those whose narration names its supplier, oldest first, as
far as it goes. What is left is open; a release that matched nothing is listed too, so the list always adds up to
what was read. For each open accrual, the invoice its narration names says whether it is still owed (no bill yet)
or should already have been reversed (the bill is in).
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core.models import Account, Document, DocumentLine, Journal, JournalLine

from .scan import invoice_refs
from .util import month_start

ZERO = Decimal("0")
REVERSAL = re.compile(r"^\s*revers(?:al|ed|ing)?\s*(?:of)?\s*:?\s*", re.I)


def xero_accruals(accrual, period_end):
    """{account: amount} accrued in Xero in the month ending ``period_end``: cost lines of manual journals dated in
    the month that credit the accrual account, reversals left out."""
    ids = list(accrual.accounts().values_list("pk", flat=True))
    journals = (Journal.objects.filter(organisation=accrual.organisation, status=Journal.Status.POSTED,
                                       date__range=(month_start(period_end), period_end), lines__account__in=ids)
                .distinct().prefetch_related("lines__account"))
    found = defaultdict(Decimal)
    for journal in journals:
        if REVERSAL.match(journal.narration or ""):
            continue
        for line in journal.lines.all():
            if line.account_id not in ids and line.account.account_class == Account.AccountClass.EXPENSE \
                    and line.amount > 0:
                found[line.account] += line.amount
    return dict(found)


def compare(journal):
    """Rows (account, expected, accrued in Xero, difference) for an Expectation Ledger journal, by account."""
    expected = defaultdict(Decimal)
    for line in journal.lines.select_related("account"):
        expected[line.account] += line.expected_amount
    actual = xero_accruals(journal.accrual, journal.period_end)
    rows = []
    for account in sorted(set(expected) | set(actual), key=lambda a: a.code):
        e, x = expected.get(account, ZERO), actual.get(account, ZERO)
        rows.append((account, e, x, e - x))
    return rows


@dataclass
class OpenAccrual:
    date: date
    narration: str
    reference: str
    amount: Decimal
    refs: list = field(default_factory=list)
    bill: Document | None = None
    released: Decimal = ZERO

    @property
    def open(self):
        return self.amount - self.released

    def status(self, as_at):
        if self.amount < 0:
            return "a debit to the accrual account that matched no accrual: check it (or an accrual before the history)"
        if self.bill is not None and self.bill.date <= as_at:
            return (f"bill {self.bill.number} dated {self.bill.date:%d %b %Y} is in: it should have been reversed "
                    "(otherwise the cost is counted twice)")
        if self.bill is not None:
            return f"bill {self.bill.number} dated {self.bill.date:%d %b %Y}, after this date: still owed at it"
        if self.refs:
            return f"invoice {', '.join(self.refs)} not found in the copied bills: check whether it's still owed"
        return "names no invoice: check whether it's still owed"


def _key(text):
    return re.sub(r"\s+", " ", REVERSAL.sub("", text or "")).strip().lower()


def open_accruals(accrual, as_at):
    """(open accruals at the end of ``as_at``, oldest first; the first date read). Only journals copied into iLA are
    read, so accruals raised before the copied history aren't seen."""
    ids = list(accrual.accounts().values_list("pk", flat=True))
    lines = (JournalLine.objects.filter(organisation=accrual.organisation, account__in=ids,
                                        journal__status=Journal.Status.POSTED, journal__date__lte=as_at)
             .select_related("journal").order_by("journal__date", "journal_id", "pk"))
    raised, releases = [], []
    for line in lines:
        text = line.journal.narration or line.description
        if line.amount < 0:
            raised.append(OpenAccrual(line.journal.date, text, line.journal.number, -line.amount, invoice_refs(text)))
        elif line.amount > 0:
            releases.append((line.journal.date, text, line.journal.number, line.amount, None))
    for line in (DocumentLine.objects.filter(organisation=accrual.organisation, account__in=ids,
                                             document__type=Document.Type.BILL, document__date__lte=as_at,
                                             document__status__in=(Document.Status.AUTHORISED, Document.Status.PAID))
                 .select_related("document__contact")):
        doc = line.document
        net = line.line_amount - (line.tax_amount if doc.line_amount_type == "inclusive" else ZERO)
        releases.append((doc.date, f"Bill {doc.number} from {doc.contact.name if doc.contact else '?'}", doc.number,
                         net, doc))
    releases.sort(key=lambda r: r[0])
    first = min([a.date for a in raised] + [r[0] for r in releases], default=None)
    unmatched = []
    for day, text, number, amount, doc in releases:
        candidates = [a for a in raised if a.open > 0 and a.date <= day]
        if doc is None:
            key = _key(text)
            match = (next((a for a in candidates if _key(a.narration) == key and a.open == amount), None)
                     or next((a for a in candidates if a.open == amount), None)
                     or next((a for a in candidates if _key(a.narration) == key), None))
            if match is not None:
                taken = min(amount, match.open)
                match.released += taken
                amount -= taken
        else:  # a bill coded to the accrual account clears the accruals it was for, oldest first
            number_key = (doc.number or "").strip().lower()
            for_it = [a for a in raised if a.open > 0 and any(
                number_key.startswith(r.lower()) or r.lower().startswith(number_key) for r in a.refs if r)]
            if not for_it and doc.contact is not None:
                words = [w for w in re.findall(r"[a-z]{4,}", doc.contact.name.lower())]
                for_it = [a for a in raised if a.open > 0 and any(w in a.narration.lower() for w in words)]
            for match in for_it:
                taken = min(amount, match.open)
                match.released += taken
                amount -= taken
                if amount <= 0:
                    break
        if amount > 0:
            unmatched.append(OpenAccrual(day, text, number or "", -amount))
    still = [a for a in raised if a.open > 0] + unmatched
    still.sort(key=lambda a: a.date)
    numbers = {ref.lower() for a in still for ref in a.refs}
    bills = {d.number.strip().lower(): d for d in Document.objects.filter(
        organisation=accrual.organisation, type=Document.Type.BILL, number__in=[n for a in still for n in a.refs])}
    if len(bills) < len(numbers):  # numbers in a different case
        for d in Document.objects.filter(organisation=accrual.organisation, type=Document.Type.BILL).exclude(number=""):
            bills.setdefault(d.number.strip().lower(), d)
    for a in still:
        a.bill = next((bills[r.lower()] for r in a.refs if r.lower() in bills), None)
    return still, first
