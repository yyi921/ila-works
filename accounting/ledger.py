"""The Expectation Ledger for accruals: each run is saved as a journal whose lines live through

    predicted → evidenced → posted → closed

- **predicted**: a run works out the amount from the profile's rule (fixed, run-rate × season, driver × rate).
- **evidenced**: a bill for the month has arrived after month end (or a person attached evidence); the line takes
  the bill's amount.
- **posted**: the journal import file was built and stamped, and a person confirmed it is in the ledger.
- **closed**: the actual bill for the month is in; the variance is recorded and fed back to the profile.

Every change is written to the line's history (when, who or what, why). The source ledger is never changed:
posting means a person imports the stamped file.
"""

import csv
import io
import statistics
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, ImportFile, SourceSystem

from .models import Expectation, LedgerJournal, Method, State
from .rules import expected
from .scan import bills_for
from .util import add_months, money, month_end, month_start

RUN = "run"
BILL_CHECK = "bill check"
XERO_JOURNAL_COLUMNS = ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount",
                        "TrackingName1", "TrackingOption1", "TrackingName2", "TrackingOption2"]


class LedgerError(Exception):
    pass


def _bill_already_posted(profile, start, end):
    """The month's own bill, already in the ledger and dated in the month: nothing to accrue. A supplier that
    usually bills after month end has last month's bill dated in this month, so it takes a second bill."""
    if profile.supplier is None:
        return None
    found = sorted(bills_for(profile.organisation, [profile.account_id], start, end, profile.supplier),
                   key=lambda b: b[0])
    needed = 2 if (profile.billing_lag_days or 0) > 0 else 1
    return found[-1] if len(found) >= needed else None


@transaction.atomic
def run_accruals(accrual, period_end, user=None, journal=None):
    """Create (or refresh, while nothing is posted) the accrual journal for the month ending ``period_end``.
    Lines a person added or changed are kept; the rest are worked out again from the profiles."""
    period_end = month_end(period_end)
    start = month_start(period_end)
    journal = journal or LedgerJournal.objects.filter(accrual=accrual, kind=LedgerJournal.Kind.ACCRUAL,
                                                      period_end=period_end).first()
    if journal is not None and journal.pk and journal.lines.filter(state__in=(State.POSTED, State.CLOSED)).exists():
        raise LedgerError(f"{journal} is already posted; change its lines in admin instead of running it again.")
    if journal is None:
        journal = LedgerJournal(organisation=accrual.organisation, accrual=accrual, period_end=period_end)
    journal.created_by = journal.created_by or user
    journal.narration = journal.narration or f"Accruals {period_end:%B %Y}"
    journal.save()
    journal.lines.filter(is_manual=False, profile__isnull=False).delete()
    kept = set(journal.lines.values_list("profile_id", flat=True))

    skipped = []
    for profile in accrual.profiles.select_related("account", "supplier").prefetch_related("tracking"):
        why = None
        amount, basis = None, ""
        if profile.pk in kept:
            continue
        if not profile.is_active:
            why = "switched off"
        elif profile.skip_if_bill_posted and (bill := _bill_already_posted(profile, start, period_end)):
            why = f"bill {bill[2]} dated {bill[0]:%d %b} already posted"
        else:
            amount, basis = expected(profile, start, period_end)
            if amount is None and profile.method != Method.EVIDENCE_ONLY:
                why = basis
            elif amount is not None and amount < accrual.materiality:
                why = f"{money(amount)} is below the {money(accrual.materiality)} materiality floor"
        if why:
            skipped.append({"profile": profile.name, "profile_id": profile.pk, "why": why})
            continue
        line = Expectation(organisation=accrual.organisation, journal=journal, profile=profile,
                           description=f"{profile.name.split(' — ')[0]} {period_end:%b %Y}"[:255],
                           account=profile.account, supplier=profile.supplier,
                           expected_amount=amount or Decimal("0"), basis=basis,
                           reverse_next_month=profile.reverse_next_month, confidence=profile.confidence)
        line.move(State.PREDICTED, user or RUN, f"Predicted by the rule: {basis}" if amount is not None
                  else "Waiting for evidence (only accrued from a bill)", amount=line.expected_amount)
        line.save()
        line.tracking.set(profile.tracking.all())
    journal.skipped = skipped
    journal.save()
    check_bills(journal)
    # Evidence-only lines with no bill found are not accruals yet.
    for line in journal.lines.filter(state=State.PREDICTED, profile__method=Method.EVIDENCE_ONLY, is_manual=False):
        journal.skipped = journal.skipped + [{"profile": line.profile.name, "profile_id": line.profile_id,
                                              "why": "no bill found after month end"}]
        line.delete()
    journal.save()
    journal.refresh_state()
    AuditLog.objects.create(organisation=accrual.organisation, user=user, action=AuditLog.Action.COMMAND,
                            input=f"run accruals {period_end:%Y-%m}", target_type="accounting.ledgerjournal",
                            target_id=str(journal.pk),
                            result={"lines": journal.lines.count(), "skipped": len(journal.skipped)})
    return journal


def _find_bill(line, used, window_days, tolerance):
    period_end = line.journal.period_end
    accounts = [line.account_id, *line.journal.accrual.accounts().values_list("pk", flat=True)]
    candidates = bills_for(line.organisation, accounts, period_end + timedelta(days=1),
                           period_end + timedelta(days=window_days), line.supplier)
    best = None
    for bill_date, contact, doc, net in candidates:
        if doc.pk in used or net <= 0:
            continue
        if line.supplier is None:  # without a supplier, only a close amount is good enough
            if not line.expected_amount or abs(net - line.expected_amount) / line.expected_amount > tolerance:
                continue
        gap = abs(net - line.expected_amount)
        if best is None or gap < best[0]:
            best = (gap, bill_date, contact, doc, net)
    return best


@transaction.atomic
def check_bills(journal, user=None):
    """Look for the month's bills. A predicted line with a bill becomes evidenced (and takes the bill amount); a
    posted line with a bill is closed with its variance, and the profile learns from it."""
    accrual = journal.accrual
    tolerance = Decimal(accrual.bill_tolerance) / 100
    lines = list(journal.lines.select_related("profile", "account", "supplier"))
    used = {e.get("document_id") for line in lines for e in line.evidence if e.get("document_id")}
    moved = 0
    for line in lines:
        if line.state not in (State.PREDICTED, State.POSTED):
            continue
        bill = next((e for e in line.evidence if e.get("type") == "bill"), None)
        if line.state == State.POSTED and bill:  # evidenced before posting: that bill is the actual
            actual = Decimal(bill["amount"])
            line.actual_amount, line.variance = actual, actual - line.expected_amount
            line.move(State.CLOSED, user or BILL_CHECK, f"Bill {bill['number']} (the evidence) is the actual",
                      actual=actual, variance=line.variance)
            _learn(line)
            line.save()
            moved += 1
            continue
        found = _find_bill(line, used, accrual.bill_window_days, tolerance)
        if not found:
            continue
        _, bill_date, contact, doc, net = found
        used.add(doc.pk)
        evidence = {"type": "bill", "document_id": doc.pk, "number": doc.number, "date": bill_date.isoformat(),
                    "supplier": contact.name if contact else "", "amount": str(net)}
        line.evidence = line.evidence + [evidence]
        if line.state == State.PREDICTED:
            before = line.expected_amount
            line.expected_amount = net
            line.confidence = 0.95
            line.move(State.EVIDENCED, user or BILL_CHECK, f"Bill {doc.number} dated {bill_date:%d %b %Y} arrived",
                      amount_before=before, amount=net)
        else:
            line.actual_amount, line.variance = net, net - line.expected_amount
            line.move(State.CLOSED, user or BILL_CHECK, f"Bill {doc.number} dated {bill_date:%d %b %Y} settled it",
                      actual=net, variance=line.variance)
            _learn(line)
        line.save()
        moved += 1
    journal.refresh_state()
    return moved


def _learn(line):
    """Feed a closed line back to its profile: the outcome, and accuracy from all outcomes so far."""
    p = line.profile
    if p is None or not line.actual_amount:
        return
    error = float(abs(line.variance) / line.actual_amount)
    p.outcomes = [o for o in p.outcomes if o.get("period") != line.journal.period_end.isoformat()] + [{
        "period": line.journal.period_end.isoformat(), "expected": str(line.expected_amount),
        "actual": str(line.actual_amount), "error": round(error, 4)}]
    p.accuracy_mape = statistics.fmean(o["error"] for o in p.outcomes[-12:])
    p.save(update_fields=["outcomes", "accuracy_mape", "updated_at"])


def import_rows(journal):
    """Rows of a Xero manual journal import: the accrual at month end and, for lines that reverse, its reversal on
    day 1 of the next month. Each line credits the first accrual account with the same tracking."""
    accrual = journal.accrual
    credit = accrual.codes[0]
    rows, fmt = [], accrual.date_format
    reversal_day = add_months(journal.period_end, 1)
    lines = list(journal.lines.filter(state__in=(State.PREDICTED, State.EVIDENCED, State.POSTED))
                 .select_related("account").prefetch_related("tracking__category"))
    if not lines:
        raise LedgerError("There are no lines to post.")
    for narration, day, sign, chosen in (
            (journal.narration or str(journal), journal.period_end, 1, lines),
            (f"Reversal: {journal.narration or journal}", reversal_day, -1,
             [line for line in lines if line.reverse_next_month])):
        for line in chosen:
            tracking = [(o.category.name, o.name) for o in line.tracking.all()][:2]
            tracking += [("", "")] * (2 - len(tracking))
            flat = [v for pair in tracking for v in pair]
            amount = line.expected_amount * sign
            for code, value in ((line.account.code, amount), (credit, -amount)):
                rows.append([narration, day.strftime(fmt), line.description, code, accrual.journal_tax_rate,
                             f"{value:.2f}", *flat])
    return rows


@transaction.atomic
def build_import_file(journal, user):
    """Build and stamp the journal import file (the same register as /jnl and invoices)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(XERO_JOURNAL_COLUMNS)
    rows = import_rows(journal)
    writer.writerows(rows)
    content = out.getvalue().encode("utf-8")
    import_file = ImportFile.objects.create(
        organisation=journal.organisation, target_system=SourceSystem.XERO, profile="Xero manual journal",
        file_name=f"{journal.narration or journal}.csv", row_count=len(rows), content=content,
        content_type="text/csv", built_by=user,
        validation={"journal": journal.pk, "total": str(journal.total_expected)})
    import_file.stamp(user, content)
    journal.import_file = import_file
    journal.save(update_fields=["import_file", "updated_at"])
    AuditLog.objects.create(organisation=journal.organisation, user=user, action=AuditLog.Action.STAMP,
                            input=f"stamp {import_file.file_name}", target_type="core.importfile",
                            target_id=str(import_file.pk), result={"sha256": import_file.sha256})
    return import_file


@transaction.atomic
def mark_posted(journal, user):
    """A person confirms the stamped file is in the ledger: open lines become posted."""
    if journal.import_file is None:
        raise LedgerError("Build and stamp the import file first.")
    now = timezone.now()
    for line in journal.lines.filter(state__in=(State.PREDICTED, State.EVIDENCED)):
        line.move(State.POSTED, user, f"Posted with {journal.import_file.file_name}")
        line.save()
    journal.posted_by, journal.posted_at = user, now
    journal.save(update_fields=["posted_by", "posted_at", "updated_at"])
    journal.import_file.status, journal.import_file.posted_by, journal.import_file.posted_at = \
        ImportFile.Status.POSTED, user, now
    journal.import_file.save()
    journal.refresh_state()
