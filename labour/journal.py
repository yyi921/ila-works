"""The pay-run journal: a pay run as Xero manual journals, one per month its days fall in, by GL account ×
department × event, with the on-costs, credited to the payroll clearing account.

Each month's journal is kept as an overlay journal (core.Adjustment, ADJ-nnnn) made by labour, bucket Expected.
While it is Expected iLA rebuilds it whenever the pay run, the event dates or the labour changes move. When the
accountant's own payroll journal for the pay run is found in Xero (narration with FNE and the pay-run end date),
iLA's journals are Superseded and iLA shows the difference by account and department.

- **Dates.** Days in a month before the pay-run end post on that month's last day; the rest on the pay-run end
  date. FNE 1/11/2026 (19 Oct – 1 Nov): 31/10/2026 and 1/11/2026, both in one file.
- **Rounding.** Lines are rounded to the cent; the cents left over go to the largest line, so the pay ties to the
  pay-run total and each on-cost to its rate × the pay-run total, as /labour shows them.
"""

import csv
import io
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from core.models import Account, Adjustment, AdjustmentLine, AuditLog

from . import allocation
from .ledger import CENT, NOT_MAPPED, ZERO, department_options, find_xero_journal, settings_for

ORIGIN = "labour"
COLUMNS = ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount", "TrackingName1",
           "TrackingOption1", "TrackingName2", "TrackingOption2"]
FROZEN = (Adjustment.Status.TO_POST, Adjustment.Status.POSTED, Adjustment.Status.REJECTED)


def _month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _cents(value):
    return value.quantize(CENT, ROUND_HALF_UP)


def _date_text(day):
    return f"{day.day}/{day.month}/{day.year}"


@dataclass
class Line:
    date: date
    account: str
    department: str
    value: object  # DimensionValue or None (Unassigned)
    amount: Decimal
    description: str = ""


@dataclass
class Plan:
    """A pay run's journals before they are saved: lines by date, the credit, the checks."""

    run: object
    settings: object
    lines: list
    first_day: dict  # journal date -> first day worked in it
    last_day: dict
    target: Decimal  # pay-run total + on-costs
    problems: list = field(default_factory=list)

    @property
    def dates(self):
        return sorted({line.date for line in self.lines})

    def total(self, day=None):
        return sum((line.amount for line in self.lines if day is None or line.date == day), ZERO)

    def narration(self):
        return (self.settings.narration or "Payroll Journal: {period_end}").replace(
            "{period_end}", _date_text(self.run.period_end))

    def description(self, day):
        return (f"Pay run ending {self.run.period_end:%d/%m/%Y}: days "
                f"{self.first_day[day]:%d/%m}–{self.last_day[day]:%d/%m}")


def _journal_date(run, day):
    end = _month_end(day)
    return end if end < run.period_end else run.period_end


def plan(run, settings=None):
    """The pay run's journals, worked out from the allocation (nothing saved)."""
    settings = settings or settings_for(run.organisation)
    org = run.organisation
    days = run.lines.values_list("date_from", "date_to")
    first = min([run.period_start] + [d for d, _ in days])
    last = max([run.period_end] + [d for _, d in days])
    m = allocation.allocate_runs(org, [run], first, last)
    costs = [c for c in settings.on_costs.all() if c.rate is not None]

    exact = defaultdict(lambda: defaultdict(lambda: ZERO))  # group -> (date, account, department, value id) -> $
    first_day, last_day = {}, {}
    for p in m.pieces:
        day = _journal_date(run, p.day)
        first_day[day] = min(first_day.get(day, p.day), p.day)
        last_day[day] = max(last_day.get(day, p.day), p.day)
        gl = p.component.gl_account or NOT_MAPPED
        department = p.cost_account.department
        for value, share in p.shares:
            exact["pay"][(day, gl, department, value)] += p.amount * share
            for cost in costs:
                exact[cost.pk][(day, cost.account, department, value)] += p.amount * share * cost.rate

    targets = {"pay": run.total, **{c.pk: _cents(run.total * c.rate) for c in costs}}
    merged = defaultdict(lambda: ZERO)
    for group, amounts in exact.items():
        rounded = {key: _cents(amount) for key, amount in amounts.items()}
        left = targets[group] - sum(rounded.values(), ZERO)
        if left and rounded:
            biggest = max(rounded, key=lambda k: (abs(rounded[k]), k[0], k[1]))
            rounded[biggest] += left
        for key, amount in rounded.items():
            merged[key] += amount
    values = m.values
    lines = [Line(day, account, department, values.get(value_id), amount)
             for (day, account, department, value_id), amount in merged.items() if amount]
    lines.sort(key=lambda l: (l.date, l.account, l.department, l.value.code if l.value else "~"))
    out = Plan(run, settings, lines, first_day, last_day, sum(targets.values(), ZERO))
    out.problems = _checks(out, m, [c for c in settings.on_costs.all() if c.rate is None])
    return out


def _checks(p, m, unrated):
    """What stops the import file being downloaded."""
    org, settings = p.run.organisation, p.settings
    problems = []
    unmapped_accounts = sorted({str(x.cost_account) for x in m.pieces if not x.cost_account.department})
    if unmapped_accounts:
        problems.append("Cost accounts without a department: " + ", ".join(unmapped_accounts) + ".")
    options = department_options(org)
    wrong = sorted({line.department for line in p.lines if line.department and options
                    and line.department not in options})
    if wrong:
        problems.append("Not a department in Xero: " + ", ".join(wrong) + ".")
    unmapped = sorted({x.component.code for x in m.pieces if not x.component.gl_account})
    if unmapped:
        problems.append("Pay components without a GL account: " + ", ".join(unmapped) + ".")
    codes = {line.account for line in p.lines} | ({settings.credit_account} if settings.credit_account else set())
    known = set(Account.objects.filter(organisation=org, code__in=codes, is_active=True)
                .values_list("code", flat=True))
    missing = sorted(codes - known - {NOT_MAPPED})
    if missing:
        problems.append("Not an account in Xero's chart: " + ", ".join(missing) + ".")
    if not settings.credit_account:
        problems.append("No credit account in Labour settings (e.g. 2350 Payroll Clearing).")
    if unrated:
        problems.append("On-cost rate not set: " + ", ".join(c.name for c in unrated) + ".")
    if settings.event_dimension and not _event_tracking(settings):
        problems.append(f"The {settings.event_dimension.name} dimension isn't linked to a Xero tracking category.")
    no_option = sorted({line.value.code for line in p.lines if line.value and not _option(line.value)})
    if no_option:
        problems.append("Not an Event option in Xero yet (add it in Xero, then refresh): " + ", ".join(no_option)
                        + ".")
    if p.total() != p.target:
        problems.append(f"The journal ({p.total():,.2f}) doesn't tie to the pay run with on-costs "
                        f"({p.target:,.2f}).")
    return problems


def _event_tracking(settings):
    dimension = settings.event_dimension
    return dimension.tracking_category.name if dimension and dimension.tracking_category_id else ""


def _option(value):
    option = value.tracking_option if value.tracking_option_id else None
    return option.name if option is not None and option.is_active else ""


# --- Saving as overlay journals ----------------------------------------------------------------------------------

def journals(run):
    return Adjustment.objects.filter(organisation=run.organisation, origin=ORIGIN, origin_ref=str(run.pk)) \
        .prefetch_related("lines").order_by("date", "number")


def _rows(p, day):
    """(account, description, amount, name1, option1, name2, option2, value) for one journal, credit last."""
    settings = p.settings
    event_name = _event_tracking(settings)
    rows = []
    for line in (x for x in p.lines if x.date == day):
        department = line.department if settings.department_tracking else ""
        option = (_option(line.value) or line.value.code) if line.value else ""
        rows.append((line.account, p.description(day), line.amount,
                     settings.department_tracking if department else "", department,
                     event_name if option else "", option, line.value))
    rows.append((settings.credit_account or NOT_MAPPED, p.description(day), -p.total(day), "", "", "", "", None))
    return rows


@transaction.atomic
def build(run, user=None):
    """Save the pay run's journals as overlay journals (one per month) and mark them superseded when the
    accountant's payroll journal is in Xero. Journals already approved, posted or rejected are left alone.
    Returns (Plan, [Adjustment])."""
    settings = settings_for(run.organisation)
    posted = find_xero_journal(run, settings)
    if posted != run.xero_journal:
        run.xero_journal = posted
        run.save(update_fields=["xero_journal", "updated_at"])
    p = plan(run, settings)
    existing = list(journals(run))
    if any(a.status in FROZEN for a in existing):
        return p, existing
    by_date = {a.date: a for a in existing}
    status = Adjustment.Status.SUPERSEDED if posted else Adjustment.Status.EXPECTED
    for day in p.dates:
        adj = by_date.pop(day, None) or Adjustment(organisation=run.organisation, origin=ORIGIN,
                                                   origin_ref=str(run.pk), date=day, made_by=user)
        adj.period_end, adj.narration, adj.evidence = _month_end(day), p.narration()[:255], run.file_name[:255]
        adj.status, adj.superseded_by = status, posted
        adj.save()
        rows = _rows(p, day)
        have = [(l.account_code, l.description, l.amount, l.tracking_name1, l.tracking_option1, l.tracking_name2,
                 l.tracking_option2, l.value_id) for l in adj.lines.all()]
        if have != [r[:7] + (r[7].pk if r[7] else None,) for r in rows]:
            adj.lines.all().delete()
            AdjustmentLine.objects.bulk_create(
                AdjustmentLine(adjustment=adj, line_number=i, account_code=r[0], description=r[1][:255],
                               amount=r[2], tracking_name1=r[3], tracking_option1=r[4], tracking_name2=r[5],
                               tracking_option2=r[6], value=r[7]) for i, r in enumerate(rows, 1))
    for stale in by_date.values():
        stale.delete()
    return p, list(journals(run))


def rebuild_touching(org, start, end):
    """Rebuild the journals of every pay run with days in ``start``..``end`` (after a labour change)."""
    from .ledger import runs_touching  # noqa: PLC0415

    for run in runs_touching(org, start, end):
        build(run)


# --- The import file and the comparison with Xero ----------------------------------------------------------------

def import_csv(run, adjustments, settings=None):
    """The Xero manual-journal import file for the pay run's journals."""
    settings = settings or settings_for(run.organisation)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for adj in adjustments:
        for line in adj.lines.all():
            writer.writerow([adj.narration, f"{adj.date:%d/%m/%Y}", line.description, line.account_code,
                             settings.tax_rate, f"{line.amount:.2f}", line.tracking_name1, line.tracking_option1,
                             line.tracking_name2, line.tracking_option2])
    return out.getvalue().encode("utf-8")


def file_name(run):
    return f"Pay-run journal {run.period_end:%Y-%m-%d}.csv"


def log_download(run, user, adjustments):
    AuditLog.objects.create(organisation=run.organisation, user=user, action=AuditLog.Action.DOWNLOAD,
                            input=f"labour journal {file_name(run)}", target_type="labour.payrun",
                            target_id=str(run.pk), result={"journals": [a.reference for a in adjustments]})


def xero_difference(run, adjustments, settings=None):
    """[(account, department, iLA, Xero, difference)] between iLA's journals for the pay run and the payroll
    journal posted in Xero, the credit account left out; None when it isn't in Xero."""
    settings = settings or settings_for(run.organisation)
    journal = run.xero_journal
    if journal is None:
        return None
    ila, xero = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for adj in adjustments:
        for line in adj.lines.all():
            if line.account_code != settings.credit_account:
                ila[(line.account_code, line.tracking_option1)] += line.amount
    category = (settings.department_tracking or "").lower()
    for line in journal.lines.select_related("account").prefetch_related("tracking__category"):
        if line.account.code == settings.credit_account:
            continue
        department = next((o.name for o in line.tracking.all() if o.category.name.lower() == category), "")
        xero[(line.account.code, department)] += line.amount
    names = dict(Account.objects.filter(organisation=run.organisation, code__in={k[0] for k in ila | xero})
                 .values_list("code", "name"))
    rows = [(f"{code} {names.get(code, '')}".strip(), department, ila.get((code, department), ZERO),
             xero.get((code, department), ZERO), ila.get((code, department), ZERO) - xero.get((code, department), ZERO))
            for code, department in sorted(set(ila) | set(xero))]
    return [r for r in rows if r[2] or r[3]]


def upload_notes(run, user=None):
    """(summary, problems) after an upload: the pay run's journals and what stops the import file."""
    p, adjustments = build(run, user)
    status = adjustments[0].get_status_display() if adjustments else ""
    text = "Pay-run journal: " + ", ".join(f"{a.reference} {a.date:%d/%m/%Y} {a.debit_total:,.2f}"
                                           for a in adjustments) + f" ({status}; see Labour view → Journal)."
    return text, p.problems
