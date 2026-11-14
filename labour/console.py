"""/labour in the console: upload a pay-run file (<<) or list the pay runs.

``/labour << breakdown.xlsx``  reads the file into the labour ledger, then shows what it adds up to by cost account
                               and against the payroll journal posted in Xero for the same pay run.
``/labour [--<period>]``       lists the pay runs (touching the period, if given).
"""

from datetime import timedelta

from django.urls import reverse

from console.output import DATE, MONEY, NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from console.registry import CommandError, read_params
from xero.api import XeroAPIError
from xero.sync import copy_period

from . import ledger
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
    return _list(ctx, params.period)


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
