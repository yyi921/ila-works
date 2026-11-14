"""The labour mappings to and from CSV files:

- Cost accounts: Code, Name, Department, Can go to an event. The cost account decides the Xero department and
  whether its cost can be shared out by event.
- Pay components: Code, Description, GL account. The pay component (Micropay's Pay Comp/Add Ded Code) decides the
  GL account.

Uploading is all or nothing: every row is checked first (departments against Xero's department tracking, GL
accounts against the chart), and nothing is saved if any row has a problem. A row not in the file is left as it
is, and an empty cell keeps what's saved.
"""

import csv
import io
from dataclasses import dataclass, field

from django.db import transaction

from core.models import Account, AuditLog

from .ledger import department_options, settings_for
from .models import CostAccount, PayComponent

YES, NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0"}
MAX_ROWS = 2000


@dataclass(frozen=True)
class Spec:
    model: type
    what: str  # "cost accounts"
    headings: tuple  # (heading, field) in file order
    aliases: dict  # other headings (lower case) -> field
    example: tuple
    retired: dict = field(default_factory=dict)  # old heading (lower case) -> why it's no longer read

    def row(self, obj):
        return [_cell(getattr(obj, f)) for _, f in self.headings]


def _cell(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


COST_ACCOUNTS = Spec(
    CostAccount, "cost accounts",
    (("Code", "code"), ("Name", "name"), ("Department", "department"), ("Can go to an event", "taggable")),
    {"cost account code": "code", "cost account": "code", "description": "name", "cost account description": "name",
     "taggable": "taggable", "event": "taggable"},
    ("700-3000", "TECC - F&B Service", "Food and Beverage Services", "yes"),
    {"gl account": "GL accounts now come from the pay component (Labour → Pay components)",
     "gl": "GL accounts now come from the pay component (Labour → Pay components)"},
)
PAY_COMPONENTS = Spec(
    PayComponent, "pay components",
    (("Code", "code"), ("Description", "description"), ("GL account", "gl_account")),
    {"pay comp/add ded code": "code", "pay component": "code", "pay comp/add ded desc": "description",
     "name": "description", "gl": "gl_account", "account": "gl_account", "gl code": "gl_account"},
    ("SAT50", "Saturday Loading - 50%", "6345"),
)


@dataclass
class Imported:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    unchanged: int = 0
    problems: list = field(default_factory=list)


def to_csv(spec, org):
    """The organisation's rows as CSV text (with a byte-order mark so Excel reads it as UTF-8). With none yet, the
    headings and an example row."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([h for h, _ in spec.headings])
    objects = spec.model.objects.filter(organisation=org).order_by("code")
    for obj in objects:
        writer.writerow(spec.row(obj))
    if not objects:
        writer.writerow(spec.example)
    return "﻿" + out.getvalue()


def import_csv(spec, org, user, text, file_name=""):
    """Check every row of ``text`` and, if none has a problem, create or update the organisation's rows."""
    result = Imported()
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not rows:
        result.problems.append("The file is empty.")
        return result
    columns = {h.lower(): f for h, f in spec.headings} | spec.aliases
    names = ", ".join(h for h, _ in spec.headings)
    index = {}
    for i, raw in enumerate(rows[0]):
        heading = raw.strip().lower()
        name = columns.get(heading)
        if name and name in index:
            result.problems.append(f'Two columns give the {name.replace("_", " ")}: keep one.')
        elif name:
            index[name] = i
        elif heading in spec.retired:
            result.problems.append(f'Remove the "{raw.strip()}" column: {spec.retired[heading]}.')
        elif heading:
            result.problems.append(f'Unknown column "{raw.strip()}". The columns are: {names}.')
    if "code" not in index:
        result.problems.append('The file needs a "Code" column.')
    if len(rows) > MAX_ROWS + 1:
        result.problems.append(f"The file has {len(rows) - 1} rows; the most is {MAX_ROWS}.")
    if result.problems:
        return result

    settings_for(org)  # so Xero's department tracking is known
    departments = department_options(org)
    by_lower = {d.lower(): d for d in departments}
    chart = set(Account.objects.filter(organisation=org).exclude(code="").values_list("code", flat=True))
    existing = {o.code.lower(): o for o in spec.model.objects.filter(organisation=org)}

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
        for name in index:
            value = cell(row, name)
            if name == "code" or not value:
                continue
            if name == "department":
                if departments and value.lower() not in by_lower:
                    result.problems.append(f"Row {n} ({code}): {value} isn't one of Xero's departments ("
                                           + ", ".join(departments) + ").")
                value = by_lower.get(value.lower(), value)
            elif name == "gl_account":
                if chart and value not in chart:
                    result.problems.append(f"Row {n} ({code}): GL account {value} isn't in the chart of accounts.")
            elif name == "taggable":
                if value.lower() not in YES | NO:
                    result.problems.append(f"Row {n} ({code}): Can go to an event must be yes or no, not {value}.")
                value = value.lower() in YES
            changes[name] = value
        planned.append((code, changes))
    if result.problems:
        return result

    with transaction.atomic():
        for code, changes in planned:
            obj = existing.get(code.lower())
            if obj is None:
                spec.model.objects.create(organisation=org, code=code, **changes)
                result.created.append(code)
            elif any(getattr(obj, k) != v for k, v in changes.items()):
                for k, v in changes.items():
                    setattr(obj, k, v)
                obj.save()
                result.updated.append(obj.code)
            else:
                result.unchanged += 1
        AuditLog.objects.create(organisation=org, user=user, action=AuditLog.Action.COMMAND,
                                input=f"labour {spec.what} from {file_name}",
                                target_type=f"labour.{spec.model._meta.model_name}",
                                result={"created": result.created, "updated": result.updated,
                                        "unchanged": result.unchanged})
    return result
