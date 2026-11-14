"""A month's labour: what the pay runs say against what's in Xero, the event shares /splittag --labour uses, and
the estimate for days no pay run covers yet.

- **Labour per pay runs**: the month's days of every pay run touching it, with on-costs (the Labour view's total).
- **In Xero now**: the month's lines on the labour accounts (Review settings → Labour accounts; blank: the
  accounts the pay-run journals post to), every type: journals, bills, bank.
- **Stub estimate**: days of the month after the last pay run, estimated from the last two pay runs: their
  average cost per day on days with an event (event or bump day) and on days without, applied to the stub days by the
  event calendar. Kept as an overlay journal (ADJ, Expected) by account and department; when a pay run covering
  those days arrives, its journal replaces the estimate.
"""

import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from core.models import Adjustment, AdjustmentLine

from . import allocation, journal
from .ledger import CENT, NOT_MAPPED, ZERO, runs_touching, settings_for
from .models import PayRun

ESTIMATE = "labour-estimate"
SAMPLE_RUNS = 2


def _cents(value):
    return value.quantize(CENT, ROUND_HALF_UP)


# --- Accounts ---------------------------------------------------------------------------------------------------

def parse_accounts(text):
    """"6300-6380; 6400, 5000" -> [["6300", "6380"], ["6400", "6400"], ["5000", "5000"]]."""
    ranges = []
    for part in re.split(r"[;,]", text or ""):
        part = part.strip()
        if part:
            low, _, high = (p.strip() for p in part.partition("-")) if "-" in part else (part, "", part)
            ranges.append([low, high or low])
    return ranges


def account_in(code, ranges):
    code = (code or "").strip()
    for low, high in ranges:
        if code.isdigit() and low.isdigit() and high.isdigit():
            if int(low) <= int(code) <= int(high):
                return True
        elif low.lower() <= code.lower() <= high.lower():
            return True
    return False


def labour_accounts(org):
    """(ranges, label, how chosen): Review settings' Labour accounts, else the accounts the pay-run journals post
    to (pay components' GL accounts and the on-costs)."""
    from review.models import ReviewSettings  # noqa: PLC0415

    found = ReviewSettings.objects.filter(organisation=org).first()
    if found and found.labour_accounts.strip():
        ranges = parse_accounts(found.labour_accounts)
        return ranges, found.labour_accounts.strip(), "Review settings"
    settings = settings_for(org)
    codes = {c for c in org_components(org) if c} | {c.account for c in settings.on_costs.all() if c.account}
    codes = sorted(codes, key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else 0, c))
    return [[c, c] for c in codes], ", ".join(codes), "the accounts the pay-run journals post to"


def org_components(org):
    from .models import PayComponent  # noqa: PLC0415

    return PayComponent.objects.filter(organisation=org).values_list("gl_account", flat=True)


def xero_lines(org, start, end, ranges):
    """iLA's copy of Xero: every posted line in the period on the accounts (PeriodLine, cost positive)."""
    from console.pl_by_dimension import period_lines  # noqa: PLC0415

    return [pl for pl in period_lines(org, start, end, pl_only=False) if account_in(pl.account.code, ranges)]


# --- The month ----------------------------------------------------------------------------------------------------

@dataclass
class MonthReview:
    start: object
    end: object
    accounts_label: str
    accounts_how: str
    pay_runs: Decimal  # the month's days of every pay run, with on-costs
    xero: Decimal
    journal_by_account: dict  # account -> the month's pay-run journal lines (debits)
    xero_by_account: dict
    runs: list  # [(run, month amount, in Xero)]
    not_in_xero: Decimal
    stub_days: list
    estimate: object = None  # Adjustment or None
    events: list = field(default_factory=list)  # [(value, amount)]
    unassigned: Decimal = ZERO
    departments: list = field(default_factory=list)  # [(department, employees, hours, amount)]
    m: object = None

    @property
    def estimate_total(self):
        return self.estimate.debit_total if self.estimate else ZERO

    @property
    def month_total(self):
        return self.pay_runs + self.estimate_total

    @property
    def difference(self):
        return self.month_total - self.xero

    @property
    def other(self):
        return self.difference - self.not_in_xero - self.estimate_total


def stub_days(org, start, end):
    """Days of the month after the last pay run that has days in or before it; none when that pay run ended more
    than a month before (nothing recent to estimate from)."""
    latest = PayRun.objects.filter(organisation=org, period_start__lte=end).order_by("-period_end").first()
    if latest is None or latest.period_end >= end or latest.period_end < start - timedelta(days=31):
        return []
    first = max(start, latest.period_end + timedelta(days=1))
    return [first + timedelta(days=i) for i in range((end - first).days + 1)]


def month_review(org, start, end, *, estimate=True):
    """Everything /labour --<month> and /splittag --labour show for ``start``..``end``."""
    ranges, label, how = labour_accounts(org)
    m = allocation.month(org, start, end)
    pay = sum((p.amount for p in m.pieces), ZERO)
    total = _cents(pay + m.on_cost(pay))

    journal_by_account, runs, not_in_xero = defaultdict(lambda: ZERO), [], ZERO
    for run in runs_touching(org, start, end).order_by("period_end"):
        _, adjustments = journal.build(run)
        amount = ZERO
        for adj in adjustments:
            if adj.period_end != end:
                continue
            for line in adj.lines.all():
                if line.amount > 0:
                    journal_by_account[line.account_code] += line.amount
                    amount += line.amount
        runs.append((run, amount, run.xero_journal_id is not None))
        if run.xero_journal_id is None:
            not_in_xero += amount

    xero_by_account = defaultdict(lambda: ZERO)
    for pl in xero_lines(org, start, end, ranges):
        xero_by_account[pl.account.code] += pl.amount
    journals_total = sum(journal_by_account.values(), ZERO)
    out = MonthReview(start, end, label, how, journals_total if runs else total, sum(xero_by_account.values(), ZERO), dict(journal_by_account),
                      dict(xero_by_account), runs, not_in_xero, stub_days(org, start, end), m=m)

    totals = allocation.by_event(m)
    out.events = sorted(((m.values[k], _cents(r["total"])) for k, r in totals.items() if k),
                        key=lambda e: -e[1])
    out.unassigned = _cents(totals[None]["total"]) if None in totals else ZERO
    departments = defaultdict(lambda: {"employees": set(), "hours": ZERO, "amount": ZERO})
    for p in m.pieces:
        row = departments[p.cost_account.department or "(no department)"]
        row["employees"].add(p.employee_id)
        row["hours"] += p.hours
        row["amount"] += p.amount + m.on_cost(p.amount)
    out.departments = sorted(((d, len(r["employees"]), r["hours"], _cents(r["amount"])) for d, r in
                              departments.items()), key=lambda r: -r[3])
    if estimate:
        out.estimate = build_estimate(org, start, end, out.stub_days)
    return out


# --- Shares for /splittag --labour --------------------------------------------------------------------------------

def event_shares(review):
    """[(value, amount, share)] of the month's labour (with on-costs, after people's changes); the rest is
    Unassigned."""
    total = sum((a for _, a in review.events), ZERO) + review.unassigned
    if not total:
        return []
    return [(v, a, a / total) for v, a in review.events if a > 0]


# --- The stub estimate ---------------------------------------------------------------------------------------------

def _estimates(org, end):
    return Adjustment.objects.filter(organisation=org, origin=ESTIMATE, origin_ref=f"{end:%Y-%m-%d}")


@transaction.atomic
def build_estimate(org, start, end, days):
    """The estimate for ``days`` (no pay run yet) as an Expected overlay journal dated ``end``, rebuilt each time;
    replaced (Superseded) once pay runs cover the month. None when nothing to estimate."""
    existing = _estimates(org, end).first()
    if not days:
        if existing and existing.status == Adjustment.Status.EXPECTED:
            existing.status = Adjustment.Status.SUPERSEDED
            existing.note = "Replaced by the pay-run journal for these days."
            existing.save()
        return None
    if existing and existing.status != Adjustment.Status.EXPECTED:
        return existing
    sample = list(PayRun.objects.filter(organisation=org, period_end__lt=days[0]).order_by("-period_end")
                  [:SAMPLE_RUNS])
    if not sample:
        return None
    first, last = min(r.period_start for r in sample), max(r.period_end for r in sample)
    m = allocation.allocate_runs(org, sample, first, last)
    settings = settings_for(org)
    calendar = allocation.calendar(settings.event_dimension, min(first, days[0]), max(last, days[-1]))

    def busy(day):
        return any(kind in ("event", "bump") for _, kind, _ in calendar.get(day, []))

    per_day = defaultdict(lambda: ZERO)
    mix = defaultdict(lambda: ZERO)  # (account, department) -> $
    for p in m.pieces:
        per_day[p.day] += p.amount + m.on_cost(p.amount)
        mix[(p.component.gl_account or NOT_MAPPED, p.cost_account.department)] += p.amount
        for c in settings.on_costs.all():
            if c.rate is not None:
                mix[(c.account, p.cost_account.department)] += p.amount * c.rate
    sample_days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    kinds = {True: [d for d in sample_days if busy(d)], False: [d for d in sample_days if not busy(d)]}
    rate = {k: (sum((per_day[d] for d in ds), ZERO) / len(ds)) if ds else None for k, ds in kinds.items()}
    overall = sum(per_day.values(), ZERO) / len(sample_days)
    amount = _cents(sum(((rate[busy(d)] if rate[busy(d)] is not None else overall) for d in days), ZERO))
    if amount <= 0:
        return None

    mix_total = sum(mix.values(), ZERO)
    lines = {key: _cents(amount * value / mix_total) for key, value in mix.items() if value}
    biggest = max(lines, key=lambda k: abs(lines[k]))
    lines[biggest] += amount - sum(lines.values(), ZERO)
    adj = existing or Adjustment(organisation=org, origin=ESTIMATE, origin_ref=f"{end:%Y-%m-%d}")
    busy_days = sum(1 for d in days if busy(d))
    adj.date, adj.period_end, adj.status = end, end, Adjustment.Status.EXPECTED
    adj.narration = f"Labour estimate {days[0]:%d/%m}–{days[-1]:%d/%m/%Y} (no pay run yet)"
    adj.evidence = (f"{len(days)} days ({busy_days} with an event) at the average of pay runs "
                    + ", ".join(f"{r.period_end:%d/%m}" for r in sorted(sample, key=lambda r: r.period_end)))[:255]
    adj.note = (f"Per day with an event {_cents(rate[True]) if rate[True] is not None else '-'}, "
                f"without {_cents(rate[False]) if rate[False] is not None else '-'}.")
    adj.save()
    adj.lines.all().delete()
    description = adj.narration
    rows = [(account, department, value) for (account, department), value in sorted(lines.items()) if value]
    AdjustmentLine.objects.bulk_create(
        [AdjustmentLine(adjustment=adj, line_number=i, account_code=account, description=description,
                        amount=value, tracking_name1=settings.department_tracking if department else "",
                        tracking_option1=department or "")
         for i, (account, department, value) in enumerate(rows, 1)]
        + [AdjustmentLine(adjustment=adj, line_number=len(rows) + 1,
                          account_code=settings.credit_account or NOT_MAPPED, description=description,
                          amount=-amount)])
    return adj


def month_bounds(day):
    return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])
