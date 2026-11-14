"""Cost accounts from and to a CSV file: Code, Name, Department, GL account, Can go to an event.

Uploading is all or nothing: every row is checked first (departments against Xero's department tracking, GL
accounts against the chart), and nothing is saved if any row has a problem. A cost account not in the file is left
as it is, and an empty cell keeps what's saved.
"""

import csv
import io
from dataclasses import dataclass, field

from django.db import transaction

from core.models import Account, AuditLog

from .ledger import department_options, settings_for
from .models import CostAccount

HEADINGS = ["Code", "Name", "Department", "GL account", "Can go to an event"]
COLUMNS = {  # heading (lower case) -> field
    "code": "code", "cost account code": "code", "cost account": "code",
    "name": "name", "description": "name", "cost account description": "name",
    "department": "department",
    "gl account": "gl_account", "gl": "gl_account", "account": "gl_account", "gl code": "gl_account",
    "can go to an event": "taggable", "taggable": "taggable", "event": "taggable",
}
YES, NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0"}
MAX_ROWS = 2000


@dataclass
class Imported:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    unchanged: int = 0
    problems: list = field(default_factory=list)


def to_csv(org):
    """The organisation's cost accounts as CSV text (with a byte-order mark so Excel reads it as UTF-8). With none
    yet, the headings and an example row."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADINGS)
    accounts = CostAccount.objects.filter(organisation=org).order_by("code")
    for a in accounts:
        writer.writerow([a.code, a.name, a.department, a.gl_account, "yes" if a.taggable else "no"])
    if not accounts:
        writer.writerow(["700-3000", "TECC - F&B Service", "Food and Beverage Services", "6345", "yes"])
    return "﻿" + out.getvalue()


def import_csv(org, user, text, file_name=""):
    """Check every row of ``text`` and, if none has a problem, create or update the cost accounts."""
    result = Imported()
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not rows:
        result.problems.append("The file is empty.")
        return result
    headers = [h.strip().lower() for h in rows[0]]
    index = {}
    for i, h in enumerate(headers):
        name = COLUMNS.get(h)
        if name and name in index:
            result.problems.append(f'Two columns give the {name.replace("_", " ")}: keep one.')
        elif name:
            index[name] = i
        elif h:
            result.problems.append(f'Unknown column "{rows[0][i].strip()}". The columns are: ' + ", ".join(HEADINGS)
                                   + ".")
    if "code" not in index:
        result.problems.append('The file needs a "Code" column (the payroll cost account code, e.g. 700-3000).')
    if len(rows) > MAX_ROWS + 1:
        result.problems.append(f"The file has {len(rows) - 1} rows; the most is {MAX_ROWS}.")
    if result.problems:
        return result

    settings_for(org)  # so Xero's department tracking is known
    departments = department_options(org)
    by_lower = {d.lower(): d for d in departments}
    chart = set(Account.objects.filter(organisation=org).exclude(code="").values_list("code", flat=True))
    existing = {a.code.lower(): a for a in CostAccount.objects.filter(organisation=org)}

    def cell(row, name):
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    planned, seen = [], {}
    for n, row in enumerate(rows[1:], 2):
        code = cell(row, "code")
        if not code:
            result.problems.append(f"Row {n}: Code is empty.")
            continue
        if code.lower() in seen:
            result.problems.append(f"Row {n}: {code} is also on row {seen[code.lower()]}.")
            continue
        seen[code.lower()] = n
        changes = {}
        if cell(row, "name"):
            changes["name"] = cell(row, "name")
        department = cell(row, "department")
        if department:
            if departments and department.lower() not in by_lower:
                result.problems.append(f"Row {n} ({code}): {department} isn't one of Xero's departments ("
                                       + ", ".join(departments) + ").")
            changes["department"] = by_lower.get(department.lower(), department)
        gl = cell(row, "gl_account")
        if gl:
            if chart and gl not in chart:
                result.problems.append(f"Row {n} ({code}): GL account {gl} isn't in the chart of accounts.")
            changes["gl_account"] = gl
        taggable = cell(row, "taggable").lower()
        if taggable:
            if taggable not in YES | NO:
                result.problems.append(f"Row {n} ({code}): Can go to an event must be yes or no, not "
                                       f"{cell(row, 'taggable')}.")
            changes["taggable"] = taggable in YES
        planned.append((code, changes))
    if result.problems:
        return result

    with transaction.atomic():
        for code, changes in planned:
            account = existing.get(code.lower())
            if account is None:
                CostAccount.objects.create(organisation=org, code=code, **changes)
                result.created.append(code)
            elif any(getattr(account, k) != v for k, v in changes.items()):
                for k, v in changes.items():
                    setattr(account, k, v)
                account.save()
                result.updated.append(account.code)
            else:
                result.unchanged += 1
        AuditLog.objects.create(organisation=org, user=user, action=AuditLog.Action.COMMAND,
                                input=f"labour cost accounts from {file_name}", target_type="labour.costaccount",
                                result={"created": result.created, "updated": result.updated,
                                        "unchanged": result.unchanged})
    return result
