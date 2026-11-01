"""Building an open-item ledger in the background (see jobs.py for why): copy the window from Xero unless a GL was
uploaded, read Xero's balance at the opening date and each month end, then rebuild the descriptions and postings.
The job writes its progress and outcome onto the ledger, which the working paper shows."""

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import jobs
from .models import OpenItemCategory, OpenItemLedger
from .openitems import build, records_from_copy, records_from_gl, suggest_categories
from .util import month_end

logger = logging.getLogger(__name__)
RUNNING, DONE, FAILED = jobs.RUNNING, jobs.DONE, jobs.FAILED
KINDS = ("bills", "credits", "bank", "journals")


def month_ends(first, last):
    """Month ends from the one containing ``first`` to ``last``."""
    ends, day = [], month_end(first)
    while day <= last:
        ends.append(day)
        day = month_end(day + timedelta(days=1))
    return ends


def add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return month_end(day.replace(year=year, month=month, day=1))


def lookback_start(ledger):
    """The first day read to work out iLA's own seed."""
    return add_months(ledger.opening_date, -ledger.lookback_months) + timedelta(days=1)


def xero_balance(source, account, as_at):
    """The account's balance at the end of ``as_at`` from Xero's trial balance (debit positive)."""
    _, rows = source.trial_balance(as_at)
    for row in rows:
        if row.get("account_id") == account.source_id or (account.code and row["label"].endswith(f"({account.code})")):
            values = row["values"]
            if len(values) >= 4:
                return (values[2] or Decimal("0")) - (values[3] or Decimal("0"))
            return (values[0] or Decimal("0")) - (values[1] or Decimal("0"))
    return Decimal("0")


def claim(ledger, message):
    now = timezone.now()
    busy = OpenItemLedger.objects.filter(pk=ledger.pk, job_status=RUNNING, job_started_at__gt=now - jobs.STALE_AFTER)
    if busy.exists():
        return False
    OpenItemLedger.objects.filter(pk=ledger.pk).update(job_status=RUNNING, job_message=message, job_started_at=now,
                                                       job_finished_at=None)
    return True


def _set(ledger_id, **values):
    OpenItemLedger.objects.filter(pk=ledger_id).update(**values)


def run(ledger, to, *, source_factory, copy=True):
    """Start building ``ledger`` up to ``to``. Returns False if a build is already running."""
    if not claim(ledger, "Reading from Xero…" if copy and not ledger.gl_rows else "Building…"):
        return False
    ledger_id = ledger.pk

    def work():
        try:
            row = OpenItemLedger.objects.select_related("account", "organisation").get(pk=ledger_id)
            source = source_factory(row.organisation)
            account, done = row.account, []
            first = lookback_start(row)  # the look-back explains (or, for iLA's own seed, makes) the breakdown
            if not row.gl_rows and copy:
                from xero.sync import copy_period

                copied = copy_period(row.organisation, source, KINDS, first, to)
                done.append(f"{copied.total} transactions read from Xero ({copied.added} new, {copied.updated} "
                            "changed)")
                _set(ledger_id, job_message="; ".join(done) + ". Reading balances…")
            balances = {}
            for day in [row.opening_date] + month_ends(row.start, to):
                balances[day.isoformat()] = str(xero_balance(source, account, day))
            row.opening_balance = Decimal(balances[row.opening_date.isoformat()])
            row.balances = balances
            before = Decimal("0")
            if row.seed == OpenItemLedger.Seed.ILA:
                before = xero_balance(source, account, first - timedelta(days=1))
            lookback = (records_from_gl(row.gl_rows, first, row.opening_date) if row.gl_rows
                        else records_from_copy(account, first, row.opening_date))
            records = (records_from_gl(row.gl_rows, row.start, to) if row.gl_rows
                       else records_from_copy(account, row.start, to))
            row.save(update_fields=["opening_balance", "balances", "updated_at"])
            if not OpenItemCategory.objects.filter(ledger=row).exists():
                made = suggest_categories(row, lookback + records)
                done.append(f"{len(made)} categories suggested from the accounts used (rename or merge them)")
            summary = build(row, to, records=records, lookback_records=lookback, lookback_balance=before)
            done.append(f"{summary['records']} records read; {summary['items']} descriptions, "
                        f"{summary['postings']} postings")
            _set(ledger_id, job_status=DONE, job_message="; ".join(done) + ".", job_finished_at=timezone.now())
        except Exception as exc:  # noqa: BLE001 - the outcome is shown in the working paper
            logger.exception("Open-item build failed for ledger %s", ledger_id)
            _set(ledger_id, job_status=FAILED, job_message=f"Failed: {exc}", job_finished_at=timezone.now())

    jobs.start(work)
    return True
