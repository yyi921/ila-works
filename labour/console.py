"""/labour in the console: upload a pay-run file (<<) or list the pay runs.

``/labour << breakdown.xlsx``  reads the file into the labour ledger, then shows what it adds up to by cost account
                               and against the payroll journal posted in Xero for the same pay run.
``/labour``                    lists the latest pay runs.
``/labour --<month>``          the month's labour: pay runs (their days in the month) and the estimate for days with
                               no pay run yet, against Xero; then by department, event and pay run.
"""

from datetime import timedelta

from django.urls import reverse

from console.output import DATE, MONEY, NUMBER, PCT, TEXT, Column, Figure, Figures, Note, Result, Table
from console.registry import CommandError, read_params
from xero.api import XeroAPIError
from xero.sync import copy_period

from . import journal, ledger
from . import review as labour_review
from .micropay import FileError
from .models import PayRun

TITLE = "/labour"
RECENT = 12


def run(ctx, cmd):
    if ctx.organisation is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    if cmd.text:
        raise CommandError(f'Put every option after "--", e.g. /labour --{cmd.text}.')
    params = read_params(cmd, ctx, period=True)
    if ctx.input_file is not None:
        if params.period:
            raise CommandError("A pay-run file says its own period: drop the period, e.g. /labour << file.xlsx.")
        return _upload(ctx, ctx.input_file)
    if params.period:
        return _review(ctx, params.period)
    return _list(ctx, None)


def _upload(ctx, upload):
    if not upload.name.lower().endswith((".xlsx", ".xlsm")):
        raise CommandError(f"{upload.name} isn't an Excel workbook. Upload the Micropay employee breakdown (.xlsx) "
                           "as it came.")
    try:
        result = ledger.upload(ctx.organisation, ctx.user, upload.name, upload.read())
    except FileError as exc:
        raise CommandError(f"{upload.name}: {exc}") from None
    run = result.run
    settings = ledger.settings_for(ctx.organisation)
    xero_note = _refresh_journals(ctx, run)
    result.learned += ledger.match_xero(run, settings)

    title = "Unchanged" if result.unchanged else "Replaced" if result.replaced else "Saved"
    out = Result(f"{TITLE} · {title}: pay run ending {run.period_end:%d/%m/%Y}", subtitle=run.file_name)
    figures = [Figure("Days worked", f"{run.period_start:%d/%m/%Y} – {run.period_end:%d/%m/%Y}", TEXT),
               Figure("Pay date", run.pay_date, DATE), Figure("Employees", run.employee_count, NUMBER),
               Figure("Lines", run.line_count, NUMBER), Figure("Total (before on-costs)", run.total)]
    if run.summary_total is not None:
        figures.append(Figure("Summary sheet", run.summary_total,
                              detail="Ties" if run.summary_total == run.total else "Doesn't tie"))
    out.add(Figures(figures))
    _cost_account_table(out, run)
    _xero_table(out, run, settings)
    if xero_note:
        out.add(Note(xero_note, "warning"))
    unset = [c for c in result.new_components if not c.gl_account]
    if unset:
        out.add(Note("New pay components without a GL account: " + ", ".join(c.code for c in unset)
                     + ". Give each a GL account in admin (Labour → Pay components).", "warning"))
        out.link = reverse("admin:labour_paycomponent_changelist") + "?mapping=missing"
        out.link_text = "Set up the new pay components"
    if result.new_cost_accounts:
        out.add(Note("New cost accounts: " + ", ".join(str(c) for c in result.new_cost_accounts)
                     + ". Give each a department and say whether it can go to an event, in admin (Labour → Cost "
                     "accounts).", "warning"))
        out.link = reverse("admin:labour_costaccount_changelist") + "?mapping=missing"
        out.link_text = "Set up the new cost accounts"
    for text in result.warnings + result.learned:
        out.add(Note(text, "warning"))
    summary, problems = journal.upload_notes(run, ctx.user)
    out.add(Note(summary))
    for text in problems:
        out.add(Note(text, "warning"))
    out.add(Note("Only each employee's code and name are kept from the file. Uploading the same pay run again "
                 "replaces it."))
    return out


def _refresh_journals(ctx, run):
    """Copy Xero's journals around the pay run's end so a just-posted payroll journal is found."""
    try:
        copy_period(ctx.organisation, ctx.source, ("journals",), run.period_end - timedelta(days=7),
                    run.period_end + timedelta(days=21))
    except XeroAPIError as exc:
        return f"Xero couldn't be read ({exc}), so the comparison uses iLA's last copy of Xero's journals."
    return ""


def _cost_account_table(out, run):
    table = out.add(Table([Column("Cost account"), Column("Department"), Column("Can go to an event"),
                           Column("Employees", NUMBER), Column("Hours", NUMBER), Column("Amount", MONEY)],
                          title="By cost account"))
    for account, employees, hours, amount in ledger.by_cost_account(run):
        table.add(str(account), account.department or "Not set", "Yes" if account.taggable else "No", employees,
                  hours, amount)
    table.add("Total", "", "", run.employee_count, None, run.total, total=True)
    table = out.add(Table([Column("Pay component"), Column("GL"), Column("Hours", NUMBER), Column("Amount", MONEY)],
                          title="By pay component"))
    for component, hours, amount in ledger.by_component(run):
        table.add(str(component), component.gl_account or "Not set", hours, amount)
    table.add("Total", "", None, run.total, total=True)


def _xero_table(out, run, settings):
    found = ledger.xero_comparison(run, settings)
    if found is None:
        gl, on_costs = ledger.by_gl(run, settings)
        table = out.add(Table([Column("GL"), Column("Amount", MONEY)],
                              title="By GL account (not in Xero yet)"))
        for code, amount in sorted(gl.items()):
            table.add(code, amount)
        for cost, amount in on_costs:
            table.add(f"{cost.account} {cost.name}" + (f" at {cost.rate * 100:.3f}%" if cost.rate is not None
                                                       else " (rate not set)"), amount)
        return
    rows, (ila, xero, diff) = found
    table = out.add(Table([Column("GL"), Column("Account"), Column("iLA", MONEY), Column("Xero", MONEY),
                           Column("Difference", MONEY)],
                          title=f"Against Xero: {run.xero_journal.narration.strip()}"))
    for code, name, a, b, d in rows:
        table.add(code, name, a, b, d, tone="" if not d else "warn")
    table.add("Total", "", ila, xero, diff, total=True)


def _list(ctx, period):
    runs = PayRun.objects.filter(organisation=ctx.organisation).select_related("xero_journal")
    if period:
        runs = ledger.runs_touching(ctx.organisation, period.start, period.end).select_related("xero_journal")
    else:
        runs = runs[:RECENT]
    out = Result(TITLE, subtitle=f"Pay runs touching {period.label}" if period else "Latest pay runs")
    table = out.add(Table([Column("Pay run ending", DATE), Column("Days worked from", DATE), Column("Pay date", DATE),
                           Column("Employees", NUMBER), Column("Total", MONEY), Column("In Xero"),
                           Column("Difference to Xero", MONEY)]))
    settings = ledger.settings_for(ctx.organisation)
    for run in runs:
        found = ledger.xero_comparison(run, settings) if run.xero_journal_id else None
        table.add(run.period_end, run.period_start, run.pay_date, run.employee_count, run.total,
                  "Yes" if found else "Not yet", found[1][2] if found else None)
    if not table.rows:
        out.add(Note("No pay runs yet. Upload the payroll file for a pay run with /labour << breakdown.xlsx.",
                     "warning"))
    else:
        out.add(Note("Upload another pay run with /labour << breakdown.xlsx. Each pay run's lines are in admin "
                     "(Labour → Pay runs)."))
    return out


def _review(ctx, period):
    r = labour_review.month_review(ctx.organisation, period.start, period.end)
    out = Result(f"{TITLE} · {period.label}", subtitle=f"Labour accounts: {r.accounts_label} ({r.accounts_how})")
    stub = (f"{r.stub_days[0]:%d/%m}–{r.stub_days[-1]:%d/%m}" if r.stub_days else "")
    table = out.add(Table([Column("Line"), Column("$", MONEY)]))
    table.add(f"Labour per pay runs ({period.label} days)", r.pay_runs)
    if r.stub_days:
        table.add(f"Estimate, no pay run yet ({stub})", r.estimate_total)
    table.add("Labour for the month", r.month_total, total=True)
    table.add("In Xero now", r.xero)
    table.add("Difference", r.difference, total=True)
    for run, amount, in_xero in r.runs:
        if not in_xero and amount:
            table.add(f"  of which: pay run {run.period_end:%d/%m} not in Xero", amount)
    if r.stub_days:
        table.add(f"  of which: estimate {stub}", r.estimate_total)
    table.add("  of which: other (accruals, reversals, timing, mapping)", r.other)

    accounts = out.add(Table([Column("Account"), Column("Pay-run journals", MONEY), Column("In Xero", MONEY),
                              Column("Difference", MONEY)], title="By account"))
    for code in sorted(set(r.journal_by_account) | set(r.xero_by_account)):
        a, x = r.journal_by_account.get(code, 0), r.xero_by_account.get(code, 0)
        accounts.add(code, a, x, a - x, tone="warn" if a != x else "")
    a, x = sum(r.journal_by_account.values()), sum(r.xero_by_account.values())
    accounts.add("Total", a, x, a - x, total=True)

    departments = out.add(Table([Column("Department"), Column("Employees", NUMBER), Column("Hours", NUMBER),
                                 Column("$", MONEY)], title="By department (pay runs)"))
    for name, employees, hours, amount in r.departments:
        departments.add(name, employees, hours, amount)
    departments.add("Total", None, None, sum(d[3] for d in r.departments), total=True)

    events = out.add(Table([Column("Event"), Column("$", MONEY), Column("Share", PCT)], title="By event (pay runs)"))
    event_total = sum(a for _, a in r.events) + r.unassigned
    for value, amount in r.events:
        events.add(f"{value.code} {value.name}" if value.name != value.code else value.code, amount,
                   amount / event_total if event_total else None)
    events.add("Unassigned", r.unassigned, r.unassigned / event_total if event_total else None)
    events.add("Total", event_total, 1 if event_total else None, total=True)

    runs = out.add(Table([Column("Pay run ending", DATE), Column("Days worked from", DATE),
                          Column(f"{period.label} days", MONEY), Column("In Xero")], title="By pay run"))
    for run, amount, in_xero in r.runs:
        runs.add(run.period_end, run.period_start, amount, "Yes" if in_xero else "Not yet")
    if r.estimate:
        out.add(Note(f"{r.estimate.reference}: {r.estimate.narration}, {r.estimate.evidence}. "
                     f"{r.estimate.note} Expected; replaced when the pay run arrives."))
    out.add(Note("Employees and days: Admin → Labour → Labour view. Share labour out by event: "
                 f"/splittag --labour --{period.label}."))
    out.link = reverse("admin:labour_view") + f"?organisation={ctx.organisation.pk}&month={period.start:%Y-%m}"
    out.link_text = "Open the Labour view"
    return out
