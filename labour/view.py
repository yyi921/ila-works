"""Admin → Labour → Labour view: the month's labour by employee, by day and by event, and the changes people make.

Each tab ties to the same total: the pay of every pay run touching the month, on the month's days, with on-costs
(switchable). Changes (an employee's days, or a whole day, to an event or Unassigned) are kept and win over iLA's
allocation until removed.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.http import urlencode

from core.models import AuditLog, DimensionValue, Organisation

from . import allocation, journal
from .ledger import settings_for
from .models import Employee, LabourOverride, PayRun

CENT = Decimal("0.01")
TABS = (("employee", "By employee"), ("day", "By day"), ("event", "By event"), ("journal", "Journal"))
NEAR = timedelta(days=45)  # events listed first in the pick list: within this of the month


def money(value):
    return (value or Decimal("0")).quantize(CENT, ROUND_HALF_UP)


def _month(text, org):
    """(start, end) for "2026-10", else the month of the latest pay run, else this month."""
    try:
        year, mon = (int(x) for x in (text or "").split("-"))
        start = date(year, mon, 1)
    except ValueError:
        latest = PayRun.objects.filter(organisation=org).order_by("-period_end").first()
        base = latest.period_end if latest else date.today()
        start = base.replace(day=1)
    return start, start.replace(day=monthrange(start.year, start.month)[1])


class ChangeForm(forms.Form):
    employee = forms.IntegerField(required=False, widget=forms.HiddenInput)
    date_from = forms.DateField(label="From", widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="To", widget=forms.DateInput(attrs={"type": "date"}))
    value = forms.ChoiceField(label="Event")
    note = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["value"].choices = choices

    def clean(self):
        data = super().clean()
        if data.get("date_from") and data.get("date_to") and data["date_to"] < data["date_from"]:
            raise ValidationError("To can't be before From.")
        return data


def _choices(dimension, start, end):
    """The pick list: Unassigned, then events near the month (by date), then every other active value."""
    if dimension is None:
        return [("", "Unassigned")]
    values = list(DimensionValue.objects.filter(dimension=dimension, is_active=True).select_related("dimension"))
    near = sorted((v for v in values if v.start_date and v.start_date - NEAR <= end and v.last_date + NEAR >= start),
                  key=lambda v: (v.start_date, v.code))
    rest = sorted((v for v in values if v not in near), key=lambda v: v.code.lower())
    label = lambda v: f"{v.code} · {v.name}" + (f" ({v.start_date:%d/%m}" + (  # noqa: E731
        f"–{v.last_date:%d/%m})" if v.last_date != v.start_date else ")") if v.start_date else "")
    return [("", "Unassigned")] + [(str(v.pk), label(v)) for v in near] + [(str(v.pk), label(v)) for v in rest]


def labour_view(model_admin, request):
    organisations = Organisation.objects.order_by("name")
    chosen = request.GET.get("organisation") or request.POST.get("organisation")
    if chosen:
        org = organisations.filter(pk=chosen).first()
    else:  # the organisation with the latest pay run, else the first
        latest = PayRun.objects.order_by("-period_end").select_related("organisation").first()
        org = latest.organisation if latest else organisations.first()
    if org is None:
        messages.warning(request, "There are no organisations yet.")
        return redirect("admin:index")
    start, end = _month(request.GET.get("month") or request.POST.get("month"), org)
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "employee"
    with_on_costs = request.GET.get("on_costs", "1") != "0"
    settings = settings_for(org)
    choices = _choices(settings.event_dimension, start, end)
    query = {"organisation": org.pk, "month": f"{start:%Y-%m}", "tab": tab, "on_costs": "1" if with_on_costs else "0"}

    if request.method == "POST":
        if not request.user.has_perm("labour.add_labouroverride"):
            messages.error(request, "You don't have permission to change where labour goes.")
        elif request.POST.get("remove"):
            change = LabourOverride.objects.filter(organisation=org, pk=request.POST["remove"]).first()
            if change:
                AuditLog.objects.create(organisation=org, user=request.user, action=AuditLog.Action.OVERRIDE,
                                        input=f"labour change removed: {change}", target_type="labour.labouroverride",
                                        target_id=str(change.pk))
                change.delete()
                journal.rebuild_touching(org, change.date_from, change.date_to)
                messages.success(request, f"Removed: {change}. iLA's allocation applies again.")
        else:
            form = ChangeForm(request.POST, choices=choices)
            if form.is_valid():
                data = form.cleaned_data
                employee = Employee.objects.filter(organisation=org, pk=data["employee"]).first() \
                    if data["employee"] else None
                value = DimensionValue.objects.filter(pk=data["value"]).first() if data["value"] else None
                change = LabourOverride.objects.create(organisation=org, employee=employee,
                                                       date_from=data["date_from"], date_to=data["date_to"],
                                                       value=value, note=data["note"], set_by=request.user)
                AuditLog.objects.create(organisation=org, user=request.user, action=AuditLog.Action.OVERRIDE,
                                        input=f"labour change: {change}", target_type="labour.labouroverride",
                                        target_id=str(change.pk))
                journal.rebuild_touching(org, change.date_from, change.date_to)
                messages.success(request, f"Saved: {change}.")
            else:
                messages.error(request, "Not saved: " + "; ".join(
                    e for errors in form.errors.values() for e in errors))
        if request.POST.get("employee"):
            query["employee"] = request.POST["employee"]
        return redirect(reverse("admin:labour_view") + "?" + urlencode(query) + (
            f"#employee-{request.POST['employee']}" if request.POST.get("employee") else ""))

    if request.GET.get("download"):
        return _download(request, org)
    m = allocation.month(org, start, end)
    context = {
        **model_admin.admin_site.each_context(request), "title": "Labour view", "opts": model_admin.model._meta,
        "org": org, "organisations": organisations, "month": f"{start:%Y-%m}", "month_label": f"{start:%B %Y}",
        "tabs": TABS, "tab": tab, "with_on_costs": with_on_costs, "query": query, "rates_missing": m.rates_missing,
        "on_cost_rate": f"{m.on_cost_rate * 100:.3f}%", "dimension": settings.event_dimension,
        "pay_runs": PayRun.objects.filter(pk__in={p.run_id for p in m.pieces}).order_by("period_end"),
        "can_change": request.user.has_perm("labour.add_labouroverride"),
    }
    total = sum((p.amount for p in m.pieces), Decimal("0"))
    context["total_pay"], context["total_on_costs"] = money(total), money(m.on_cost(total))
    context["total"] = money(total + (m.on_cost(total) if with_on_costs else 0))
    if tab == "employee":
        context.update(_employee_tab(m, request, choices, with_on_costs, start, end))
    elif tab == "day":
        context.update(_day_tab(m, choices, with_on_costs, start, end))
    elif tab == "event":
        context.update(_event_tab(m))
    else:
        context.update(_journal_tab(context["pay_runs"], end, context["total"] if with_on_costs else None))
    return TemplateResponse(request, "admin/labour/labour_view.html", context)


def _value_columns(m, totals):
    """Events with any amount, by start date, then code."""
    ids = {v for row in totals for v in row if v}
    return sorted((m.values[i] for i in ids), key=lambda v: (v.start_date or date.max, v.code))


def _employee_tab(m, request, choices, with_on_costs, start, end):
    totals = allocation.by_employee(m, with_on_costs)
    columns = _value_columns(m, totals.values())
    employees = {e.pk: e for e in Employee.objects.filter(pk__in=totals)}
    departments = {}
    for p in m.pieces:
        departments.setdefault(p.employee_id, {}).setdefault(p.cost_account.department or "(no department)",
                                                             Decimal("0"))
        departments[p.employee_id][p.cost_account.department or "(no department)"] += p.amount
    rows = []
    for pk, amounts in sorted(totals.items(), key=lambda kv: (employees[kv[0]].name.lower(), employees[kv[0]].code)):
        rows.append({"employee": employees[pk],
                     "department": max(departments[pk], key=departments[pk].get),
                     "cells": [money(amounts.get(v.pk)) for v in columns],
                     "unassigned": money(amounts.get(None)), "total": money(sum(amounts.values()))})
    column_totals = [money(sum((totals[e].get(v.pk, 0) for e in totals), Decimal("0"))) for v in columns]
    unassigned = money(sum((a.get(None, 0) for a in totals.values()), Decimal("0")))
    open_id = request.GET.get("employee")
    detail = None
    if open_id and open_id.isdigit() and int(open_id) in employees:
        employee = employees[int(open_id)]
        days = allocation.employee_days(m, employee.pk, with_on_costs)
        for row in days:
            row["where"] = ", ".join(
                (m.values[v].code if v else "Unassigned") + (f" {money(a):,}" if len(row["shares"]) > 1 else "")
                for v, a in sorted(row["shares"].items(), key=lambda kv: -kv[1]))
            row["amount"], row["hours"] = money(row["amount"]), row["hours"].quantize(CENT)
            row["components"] = ", ".join(sorted(row["components"]))
            row["reason"] = "; ".join(sorted(row["reasons"]))
        detail = {"employee": employee, "days": days,
                  "changes": [c for c in m.overrides if c.employee_id == employee.pk],
                  "form": ChangeForm(choices=choices, initial={"employee": employee.pk, "date_from": start,
                                                               "date_to": end})}
    return {"columns": columns, "rows": rows, "column_totals": column_totals, "unassigned_total": unassigned,
            "detail": detail}


def _day_tab(m, choices, with_on_costs, start, end):
    totals = allocation.by_day(m, with_on_costs)
    columns = _value_columns(m, [row["values"] for row in totals.values()])
    rows = []
    day = start
    while day <= end:
        row = totals.get(day)
        marks = [f"{v.code} ({'event' if kind == 'event' else 'season' if kind == 'season' else f'bump {offset:+d}'})"
                 for v, kind, offset in m.calendar.get(day, [])]
        changed = [c for c in m.overrides if not c.employee_id and c.date_from <= day <= c.date_to][:1]
        rows.append({"day": day, "marks": ", ".join(marks), "changed": changed[0] if changed else None,
                     "hours": (row["hours"] if row else Decimal("0")).quantize(CENT),
                     "cells": [money(row["values"].get(v.pk)) if row else money(0) for v in columns],
                     "unassigned": money(row["unassigned"]) if row else money(0),
                     "not_taggable": money(row["not_taggable"]) if row else money(0),
                     "total": money(row["total"]) if row else money(0)})
        day += timedelta(days=1)
    return {"columns": columns, "rows": rows,
            "column_totals": [money(sum((r["values"].get(v.pk, 0) for r in totals.values()), Decimal("0")))
                              for v in columns],
            "unassigned_total": money(sum((r["unassigned"] for r in totals.values()), Decimal("0"))),
            "not_taggable_total": money(sum((r["not_taggable"] for r in totals.values()), Decimal("0"))),
            "day_changes": [c for c in m.overrides if not c.employee_id],
            "form": ChangeForm(choices=choices, initial={"date_from": start, "date_to": start})}


def _event_tab(m):
    totals = allocation.by_event(m)
    events = sorted((k for k in totals if k), key=lambda k: (m.values[k].start_date or date.max, m.values[k].code))
    rows = []
    for key in events + ([None] if None in totals else []):
        r = totals[key]
        rows.append({"value": m.values.get(key), "employees": len(r["employees"]), "hours": r["hours"].quantize(CENT),
                     "wages": money(r["wages"]), "super": money(r["super"]), "on_costs": money(r["on_costs"]),
                     "total": money(r["total"]), "not_taggable": money(r["not_taggable"]),
                     "no_event": money(r["no_event"])})
    sums = {k: money(sum((r[k] for r in totals.values()), Decimal("0"))) for k in ("wages", "super", "on_costs",
                                                                                    "total")}
    return {"event_rows": rows, "event_sums": sums}


def _journal_tab(runs, month_end, view_total):
    """Each pay run touching the month: its journals (ADJ, one per month), the ties, the checks and, once the
    accountant's payroll journal is in Xero, the difference by account and department."""
    out, month_total = [], Decimal("0")
    for run in runs:
        p, adjustments = journal.build(run)
        rows = []
        for adj in adjustments:
            by_key = {}
            for line in adj.lines.all():
                key = (line.account_code, line.tracking_option1)
                by_key[key] = by_key.get(key, Decimal("0")) + line.amount
            debit = sum((line.amount for line in adj.lines.all() if line.amount > 0), Decimal("0"))
            if adj.period_end == month_end:
                month_total += debit
            rows.append({"adj": adj, "debit": debit, "lines": list(adj.lines.all()),
                         "summary": [(code, dept, amount) for (code, dept), amount in sorted(by_key.items())]})
        out.append({"run": run, "plan": p, "journals": rows, "ties": p.total() == p.target,
                    "difference": journal.xero_difference(run, adjustments, p.settings)})
    return {"runs": out, "month_journal_total": money(month_total), "view_total": view_total}


def _download(request, org):
    run = PayRun.objects.filter(organisation=org, pk=request.GET["download"]).first()
    if run is None:
        messages.error(request, "That pay run isn't there any more.")
        return redirect(reverse("admin:labour_view") + f"?organisation={org.pk}&tab=journal")
    p, adjustments = journal.build(run)
    if p.problems:
        messages.error(request, "The import file can't be built yet: " + " ".join(p.problems))
        return redirect(reverse("admin:labour_view") + "?" + urlencode(
            {"organisation": org.pk, "month": f"{run.period_end:%Y-%m}", "tab": "journal"}))
    response = HttpResponse(journal.import_csv(run, adjustments, p.settings), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{journal.file_name(run)}"'
    journal.log_download(run, request.user, adjustments)
    return response
