"""Saving pay runs into the labour ledger, and what they add up to: by cost account, by GL account, and against
the payroll journal posted in Xero for the same pay run."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum

from core.models import Account, AuditLog, Dimension, Journal, TrackingCategory, TrackingOption

from .micropay import read
from .models import CostAccount, Employee, LabourSettings, OnCost, PayComponent, PayLine, PayRun

ZERO = Decimal("0")
CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.00001")
NOT_MAPPED = "(not mapped)"


# --- Settings ---------------------------------------------------------------------------------------------------

def _account(org, *words):
    """The code of the first active account whose name contains any of ``words``, or ""."""
    for word in words:
        found = (Account.objects.filter(organisation=org, is_active=True, name__icontains=word)
                 .exclude(code="").order_by("code").first())
        if found:
            return found.code
    return ""


def settings_for(org):
    """The organisation's labour settings, made with guesses from its chart and tracking the first time."""
    found = LabourSettings.objects.filter(organisation=org).first()
    if found:
        return found
    with transaction.atomic():
        department = (TrackingCategory.objects.filter(organisation=org, is_active=True, name__icontains="department")
                      .order_by("name").first())
        event = (Dimension.objects.filter(organisation=org, is_active=True, name__iexact="event").first()
                 or Dimension.objects.filter(organisation=org, is_active=True,
                                             tracking_category__name__iexact="event").first())
        found = LabourSettings.objects.create(
            organisation=org, credit_account=_account(org, "payroll clearing", "wages payable"),
            department_tracking=department.name if department else "", event_dimension=event)
        for order, (name, words) in enumerate((("Payroll tax", ("payroll tax",)),
                                               ("Workcover", ("workcover", "workers comp", "workers' comp")))):
            code = _account(org, *words)
            if code:
                OnCost.objects.create(organisation=org, settings=found, name=name, account=code, order=order)
    return found


def guess_gl(org, transaction_type, description):
    """A first guess at a new pay component's GL account, from the chart: super, sick leave, annual leave, long
    service leave and allowances. Anything else is left for a person to set."""
    kind, text = (transaction_type or "").lower(), (description or "").lower()
    for words, accounts in ((("super",), ("superannuation",)), (("sick", "personal"), ("sick leave",)),
                            (("annual leave",), ("annual leave",)), (("long service",), ("long service leave",)),
                            (("allowance",), ("allowance",))):
        if any(w in kind or w in text for w in words):
            return _account(org, *accounts)
    return ""


def department_options(org):
    """The options of the organisation's department tracking category in Xero, by name (empty if none set)."""
    found = LabourSettings.objects.filter(organisation=org).first()
    if not found or not found.department_tracking:
        return []
    return list(TrackingOption.objects.filter(organisation=org, category__name__iexact=found.department_tracking,
                                              is_active=True).order_by("name").values_list("name", flat=True))


def gl_for(line):
    """The GL account a pay line posts to: its pay component's."""
    return line.pay_component.gl_account or NOT_MAPPED


# --- Saving -----------------------------------------------------------------------------------------------------

@dataclass
class Upload:
    run: PayRun
    replaced: bool = False
    unchanged: bool = False
    new_cost_accounts: list = field(default_factory=list)
    new_components: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    learned: list = field(default_factory=list)


def upload(org, user, file_name, data):
    """Read a payroll file and save it as a pay run (replacing an earlier upload of the same pay run). Raises
    micropay.FileError for a file that can't be read."""
    file_hash = hashlib.sha256(data).hexdigest()
    parsed = read(data)
    settings = settings_for(org)
    existing = PayRun.objects.filter(organisation=org, period_end=parsed.period_end).first()
    if existing and existing.file_hash == file_hash:
        result = Upload(existing, unchanged=True, warnings=parsed.warnings)
        result.learned = match_xero(existing, settings)
        return result

    with transaction.atomic():
        run = existing or PayRun(organisation=org, period_end=parsed.period_end)
        run.pay_period_id, run.period_start, run.pay_date = parsed.pay_period_id, parsed.period_start, parsed.pay_date
        run.frequency, run.file_name, run.file_hash = parsed.frequency, file_name, file_hash
        run.total, run.summary_total, run.uploaded_by = parsed.total, parsed.summary_total, user
        run.line_count = len(parsed.lines)
        run.employee_count = len({line.employee_code for line in parsed.lines})
        run.save()
        if existing:
            run.lines.all().delete()

        employees = {e.code: e for e in Employee.objects.filter(organisation=org)}
        for line in parsed.lines:
            employee = employees.get(line.employee_code)
            if employee is None:
                employee = employees[line.employee_code] = Employee.objects.create(
                    organisation=org, code=line.employee_code, name=line.employee_name,
                    employment_type=line.employment_type)
            elif (employee.name, employee.employment_type) != (line.employee_name, line.employment_type):
                employee.name, employee.employment_type = line.employee_name, line.employment_type
                employee.save(update_fields=["name", "employment_type", "updated_at"])

        accounts, new = {c.code: c for c in CostAccount.objects.filter(organisation=org)}, []
        for line in parsed.lines:
            if line.cost_account_code not in accounts:
                accounts[line.cost_account_code] = CostAccount.objects.create(
                    organisation=org, code=line.cost_account_code, name=line.cost_account_name)
                new.append(accounts[line.cost_account_code])
            elif not accounts[line.cost_account_code].name and line.cost_account_name:
                accounts[line.cost_account_code].name = line.cost_account_name
                accounts[line.cost_account_code].save(update_fields=["name", "updated_at"])

        components, new_components = {c.code: c for c in PayComponent.objects.filter(organisation=org)}, []
        seen = set()
        for line in parsed.lines:
            code = line.component_code or "(none)"
            found = components.get(code)
            if found is None:
                found = components[code] = PayComponent.objects.create(
                    organisation=org, code=code, description=line.component, transaction_type=line.transaction_type,
                    gl_account=guess_gl(org, line.transaction_type, line.component))
                new_components.append(found)
            elif code not in seen and (found.description, found.transaction_type) != (
                    line.component or found.description, line.transaction_type):
                found.description = line.component or found.description
                found.transaction_type = line.transaction_type
                found.save(update_fields=["description", "transaction_type", "updated_at"])
            seen.add(code)

        PayLine.objects.bulk_create(PayLine(
            organisation=org, run=run, employee=employees[line.employee_code],
            cost_account=accounts[line.cost_account_code],
            pay_component=components[line.component_code or "(none)"], transaction_type=line.transaction_type,
            component_code=line.component_code, component=line.component, date_from=line.date_from,
            date_to=line.date_to, hours=line.hours, amount=line.amount) for line in parsed.lines)
        AuditLog.objects.create(organisation=org, user=user, action=AuditLog.Action.COMMAND,
                                input=f"labour upload {file_name}", target_type="labour.payrun",
                                target_id=str(run.pk),
                                result={"period_end": run.period_end.isoformat(), "total": str(run.total),
                                        "lines": run.line_count, "replaced": bool(existing)})

    result = Upload(run, replaced=bool(existing), new_cost_accounts=new, new_components=new_components,
                    warnings=parsed.warnings)
    result.learned = match_xero(run, settings)
    return result


# --- Xero -------------------------------------------------------------------------------------------------------

def _date_texts(day):
    return {f"{day.day}/{day.month}/{day.year}", f"{day:%d/%m/%Y}", f"{day.day:02d}/{day.month}/{day.year}",
            f"{day.day}/{day.month:02d}/{day.year}"}


def find_xero_journal(run, settings):
    """The posted payroll journal in iLA's copy of Xero for ``run``: narration containing the settings' text and
    the pay run's end date (e.g. FNE: 18/10/2026)."""
    if not settings.journal_match:
        return None
    candidates = Journal.objects.filter(
        organisation=run.organisation, status=Journal.Status.POSTED, narration__icontains=settings.journal_match,
        date__range=(run.period_end - timedelta(days=7), run.period_end + timedelta(days=21)))
    texts = _date_texts(run.period_end)
    return next((j for j in candidates.order_by("date", "pk") if any(t in j.narration for t in texts)), None)


def match_xero(run, settings):
    """Link ``run`` to its payroll journal in Xero (if posted) and learn on-cost rates not yet set. Returns notes
    about what was learned."""
    journal = find_xero_journal(run, settings)
    if journal != run.xero_journal:
        run.xero_journal = journal
        run.save(update_fields=["xero_journal", "updated_at"])
    if journal is None or not run.total:
        return []
    by_account = _journal_by_account(journal)
    notes = []
    for cost in settings.on_costs.all():
        posted = by_account.get(cost.account)
        if posted is None:
            continue
        rate = (posted / run.total).quantize(RATE_PLACES)
        if cost.rate is None:
            cost.rate = rate
            cost.save(update_fields=["rate", "updated_at"])
            notes.append(f"{cost.name} rate set to {rate * 100:.3f}%, learned from {journal.narration.strip()}. "
                         "Change it in Labour settings if that's not right.")
        elif abs(rate - cost.rate) >= Decimal("0.0001"):
            notes.append(f"{cost.name} in Xero is {rate * 100:.3f}% of this pay run; the setting is "
                         f"{cost.rate * 100:.3f}%.")
    return notes


def _journal_by_account(journal):
    totals = defaultdict(lambda: ZERO)
    for line in journal.lines.select_related("account"):
        totals[line.account.code] += line.amount
    return dict(totals)


# --- What a pay run adds up to ----------------------------------------------------------------------------------

def by_cost_account(run):
    """[(CostAccount, employees, hours, amount)] for the pay run."""
    rows = (run.lines.values("cost_account").annotate(employees=Count("employee", distinct=True),
                                                      hours=Sum("hours"), amount=Sum("amount"))
            .order_by("cost_account__code"))
    accounts = {c.pk: c for c in CostAccount.objects.filter(pk__in=[r["cost_account"] for r in rows])}
    return [(accounts[r["cost_account"]], r["employees"], r["hours"] or ZERO, r["amount"] or ZERO) for r in rows]


def by_component(run):
    """[(PayComponent, hours, amount)] for the pay run."""
    rows = (run.lines.values("pay_component").annotate(hours=Sum("hours"), amount=Sum("amount"))
            .order_by("pay_component__code"))
    components = {c.pk: c for c in PayComponent.objects.filter(pk__in=[r["pay_component"] for r in rows])}
    return [(components[r["pay_component"]], r["hours"] or ZERO, r["amount"] or ZERO) for r in rows]


def by_gl(run, settings=None):
    """{GL account: amount} for the pay run's lines, then each on-cost on its account (None when its rate isn't
    set yet)."""
    settings = settings or settings_for(run.organisation)
    totals = defaultdict(lambda: ZERO)
    for line in run.lines.select_related("pay_component"):
        totals[gl_for(line)] += line.amount
    on_costs = [(cost, (run.total * cost.rate).quantize(CENT) if cost.rate is not None else None)
                for cost in settings.on_costs.all()]
    return dict(totals), on_costs


def xero_comparison(run, settings=None):
    """[(GL code, account name, iLA, Xero, difference)] against the payroll journal in Xero, the credit account
    left out; plus the totals row. None when the journal isn't in Xero yet."""
    settings = settings or settings_for(run.organisation)
    journal = run.xero_journal
    if journal is None:
        return None
    lines, on_costs = by_gl(run, settings)
    ila = defaultdict(lambda: ZERO, lines)
    for cost, amount in on_costs:
        ila[cost.account] += amount or ZERO
    xero = {code: amount for code, amount in _journal_by_account(journal).items()
            if code != settings.credit_account}
    names = dict(Account.objects.filter(organisation=run.organisation, code__in=set(ila) | set(xero))
                 .values_list("code", "name"))
    rows = [(code, names.get(code, ""), ila.get(code, ZERO), xero.get(code, ZERO),
             ila.get(code, ZERO) - xero.get(code, ZERO)) for code in sorted(set(ila) | set(xero))]
    total = (sum(r[2] for r in rows), sum(r[3] for r in rows))
    return rows, (total[0], total[1], total[0] - total[1])


def runs_touching(org, start, end):
    return PayRun.objects.filter(organisation=org, period_start__lte=end, period_end__gte=start)
