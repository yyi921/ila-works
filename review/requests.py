"""Review requests: a person's prompt, turned once into a saved check that code runs every month.

The language model is used only at the trial run, to turn the prompt into a check made of iLA's building blocks
(SCHEMA): which transaction lines (P&L categories, account codes, types, contact or text, an iLA dimension
assigned or not, a minimum amount), how to group them, an optional comparison with last month or last year, and when
a group or line becomes a finding. iLA checks the answer (``clean``) and runs it with code (``run_spec``) on the
transactions copied from Xero, so every /review gives the same kind of answer. What it can't do is said, not
guessed: the model fills ``cannot`` and iLA shows it with the trial result.

Amounts are net of tax, in the base currency, signed as the P&L shows them (income and costs both positive).
"""

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounting import jobs
from console.llm import LLMError, call_tool
from console.periods import add_months, month_period
from console.pl_by_dimension import UNASSIGNED, _tags, period_lines
from core.models import BankTransaction, Dimension, DimensionValue, Document
from core.tagging import split_amount
from xero.sync import KINDS, copy_period

from .models import RequestStatus, ReviewRequest, ReviewSettings

ZERO = Decimal("0")
CATEGORIES = ("revenue", "other_income", "cost_of_sales", "expenses")
TYPES = ("sales_invoice", "sales_credit", "bill", "supplier_credit", "receive_money", "spend_money", "journal")
GROUPS = ("line", "transaction", "contact", "account", "dimension", "month")
COMPARES = ("none", "last_month", "last_year")
TYPE_LABEL = {"sales_invoice": "Sales invoice", "sales_credit": "Sales credit", "bill": "Bill",
              "supplier_credit": "Supplier credit", "receive_money": "Receive money", "spend_money": "Spend money",
              "journal": "Journal"}
MAX_ROWS = 200

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "Short name for the check, e.g. 'Revenue not assigned to an event'."},
        "plain": {"type": "string", "description": "The check in one or two plain-English sentences, exactly as it "
                                                   "will run."},
        "cannot": {"type": "string", "description": "Any part of the request these building blocks can't do, and "
                                                    "why, in plain English. Empty if it can all be done."},
        "lines": {"type": "object", "properties": {
            "categories": {"type": "array", "items": {"type": "string", "enum": list(CATEGORIES)},
                           "description": "P&L categories to include; empty = all."},
            "account_codes": {"type": "array", "items": {"type": "string"},
                              "description": "Account codes or ranges like '6300-6380'; empty = any."},
            "exclude_account_codes": {"type": "array", "items": {"type": "string"}},
            "types": {"type": "array", "items": {"type": "string", "enum": list(TYPES)},
                      "description": "Transaction types; empty = all."},
            "contact_contains": {"type": "array", "items": {"type": "string"}},
            "text_contains": {"type": "array", "items": {"type": "string"},
                              "description": "Words in the line description, reference or narration."},
            "dimension": {"type": "string", "description": "Name of an iLA dimension (e.g. Event), or empty."},
            "dimension_filter": {"type": "string", "enum": ["any", "unassigned", "assigned"]},
            "min_amount": {"type": ["number", "null"], "description": "Only lines at least this big, either way."},
        }},
        "group_by": {"type": "string", "enum": list(GROUPS)},
        "measure": {"type": "string", "enum": ["sum", "ratio"]},
        "ratio": {"type": "object", "description": "For measure 'ratio': numerator ÷ denominator per group, in %.",
                  "properties": {
                      "numerator_codes": {"type": "array", "items": {"type": "string"}},
                      "denominator_codes": {"type": "array", "items": {"type": "string"}},
                      "numerator_label": {"type": "string", "description": "Short column name, e.g. 'Recovered'."},
                      "denominator_label": {"type": "string", "description": "Short column name, e.g. 'Cost'."},
                      "below": {"type": ["number", "null"], "description": "Raise when the ratio is below this %."},
                      "above": {"type": ["number", "null"], "description": "Raise when the ratio is above this %."}}},
        "compare": {"type": "string", "enum": list(COMPARES),
                    "description": "Compare each group with the same group last month or last year. Not for "
                                   "group_by line or transaction."},
        "raise_when": {"type": "object", "properties": {
            "amount_over": {"type": ["number", "null"], "description": "Raise when the amount is over this, either "
                                                                      "way."},
            "change_over": {"type": ["number", "null"], "description": "With compare: raise when the change is over "
                                                                      "this, either way."},
            "new_only": {"type": "boolean", "description": "With compare: only groups with nothing in the compared "
                                                          "month."}}},
        "limit": {"type": "integer", "description": "Most rows to show (default 50)."},
        "action": {"type": "string", "description": "The iLA command that fixes a finding, e.g. '/tag' or "
                                                    "'/accrue', or empty."},
    },
    "required": ["title", "plain", "cannot", "lines", "group_by", "measure", "compare", "raise_when"],
}

SYSTEM = """You turn an accountant's review request into a check that iLA runs every month on the P&L.

iLA can read every P&L line of the month from the accounting system's sales invoices, sales credits, bills, supplier
credits, receive-money and spend-money bank transactions and manual journals (payroll arrives as manual journals),
with each line's date, account, contact, description, reference and amount (net of tax), and the organisation's iLA
dimensions (e.g. Event), where a line is assigned to a value or not. It can group lines, add them up, divide two
groups of accounts (a ratio) and compare with the same month last month or last year.

It can't see rosters, timesheets, hours, stock counts, budgets by line, bank statements or anything outside the
accounting system. If part of the request needs those, say so in "cannot" and do what you can.

Use only account codes from the chart given. Prefer the simplest check that answers the request. Write "plain" so a
finance person can confirm it is what they meant."""


class SpecError(Exception):
    """The worked-out check isn't usable; the message is shown as is."""


# --- Compiling (the trial run's one model call) -------------------------------------------------------------------

def compile_request(organisation, prompt, accounts):
    """The model's check for ``prompt`` (a dict, not yet cleaned)."""
    chart = "\n".join(f"{v['code']} {v['name']} ({v['category']})" for v in
                      sorted(accounts.values(), key=lambda v: v["code"]) if v.get("category") and v.get("code"))
    dims = []
    for d in Dimension.objects.filter(organisation=organisation, is_active=True):
        sample = ", ".join(d.values.values_list("name", flat=True)[:8])
        dims.append(f"{d.name}: e.g. {sample}")
    content = (f"Request: {prompt}\n\nChart of accounts (P&L):\n{chart}\n\niLA dimensions:\n"
               + ("\n".join(dims) or "none"))
    return call_tool(system=SYSTEM, content=content, tool_name="save_check",
                     description="Save the check iLA will run each month.", schema=SCHEMA)


def clean(spec, organisation):
    """The spec with defaults filled in and every value checked."""
    if not isinstance(spec, dict):
        raise SpecError("The check came back empty.")
    lines = dict(spec.get("lines") or {})
    out = {
        "title": str(spec.get("title") or "Review request")[:120],
        "plain": str(spec.get("plain") or ""),
        "cannot": str(spec.get("cannot") or ""),
        "lines": {
            "categories": [c for c in lines.get("categories") or [] if c in CATEGORIES],
            "account_codes": [str(c) for c in lines.get("account_codes") or []],
            "exclude_account_codes": [str(c) for c in lines.get("exclude_account_codes") or []],
            "types": [t for t in lines.get("types") or [] if t in TYPES],
            "contact_contains": [str(c).lower() for c in lines.get("contact_contains") or [] if str(c).strip()],
            "text_contains": [str(c).lower() for c in lines.get("text_contains") or [] if str(c).strip()],
            "dimension": str(lines.get("dimension") or ""),
            "dimension_filter": lines.get("dimension_filter") if lines.get("dimension_filter") in
            ("any", "unassigned", "assigned") else "any",
            "min_amount": _number(lines.get("min_amount")),
        },
        "group_by": spec.get("group_by") if spec.get("group_by") in GROUPS else "line",
        "measure": "ratio" if spec.get("measure") == "ratio" else "sum",
        "ratio": {},
        "compare": spec.get("compare") if spec.get("compare") in COMPARES else "none",
        "raise_when": {"amount_over": _number((spec.get("raise_when") or {}).get("amount_over")),
                       "change_over": _number((spec.get("raise_when") or {}).get("change_over")),
                       "new_only": bool((spec.get("raise_when") or {}).get("new_only"))},
        "limit": min(int(spec.get("limit") or 50), MAX_ROWS),
        "action": str(spec.get("action") or "")[:100],
    }
    if out["lines"]["dimension"]:
        if _dimension(organisation, out["lines"]["dimension"]) is None:
            raise SpecError(f'There\'s no iLA dimension called "{out["lines"]["dimension"]}".')
    elif out["group_by"] == "dimension" or out["lines"]["dimension_filter"] != "any":
        raise SpecError("The check needs a dimension (e.g. Event) to group or filter by.")
    if out["group_by"] in ("line", "transaction"):
        out["compare"] = "none"
        if out["measure"] == "ratio":
            raise SpecError("A ratio needs groups (by contact, account, dimension or month), not single lines.")
    if out["measure"] == "ratio":
        ratio = spec.get("ratio") or {}
        out["ratio"] = {"numerator_codes": [str(c) for c in ratio.get("numerator_codes") or []],
                        "denominator_codes": [str(c) for c in ratio.get("denominator_codes") or []],
                        "numerator_label": str(ratio.get("numerator_label") or "Numerator")[:40],
                        "denominator_label": str(ratio.get("denominator_label") or "Denominator")[:40],
                        "below": _number(ratio.get("below")), "above": _number(ratio.get("above"))}
        if not out["ratio"]["numerator_codes"] or not out["ratio"]["denominator_codes"]:
            raise SpecError("A ratio needs the accounts for both its top and bottom.")
    return out


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dimension(organisation, name):
    name = name.strip().lower()
    for d in Dimension.objects.filter(organisation=organisation, is_active=True):
        if name in (d.name.lower(), d.code.lower()):
            return d
    return None


# --- Running a check ---------------------------------------------------------------------------------------------

def _code_test(codes):
    ranges = []
    for part in codes:
        low, _, high = str(part).partition("-")
        ranges.append((low.strip(), (high or low).strip()))

    def test(code):
        for low, high in ranges:
            if low.isdigit() and high.isdigit() and code.isdigit():
                if int(low) <= int(code) <= int(high):
                    return True
            elif low <= code <= high:
                return True
        return False

    return test


def _type(parent):
    if isinstance(parent, Document):
        receivable = parent.ledger == Document.Ledger.RECEIVABLE
        if parent.type == Document.Type.CREDIT_NOTE:
            return "sales_credit" if receivable else "supplier_credit"
        return "sales_invoice" if receivable else "bill"
    if isinstance(parent, BankTransaction):
        return "receive_money" if parent.direction == BankTransaction.Direction.RECEIVE else "spend_money"
    return "journal"


def _number_of(parent):
    if isinstance(parent, Document):
        return parent.number or parent.reference
    if isinstance(parent, BankTransaction):
        return parent.reference
    return parent.number or (parent.narration or "")[:40]


def _contact(parent):
    contact = getattr(parent, "contact", None)
    return contact.name if contact else ""


def _haystack(pl_line):
    parent = pl_line.parent
    parts = [pl_line.line.description, getattr(parent, "reference", ""), getattr(parent, "number", ""),
             getattr(parent, "narration", "")]
    return " ".join(p for p in parts if p).lower()


def _lines(organisation, spec, start, end):
    """[(PeriodLine, amount, dimension value name or None)] passing the spec's filters: a line split across values
    gives one entry per value (and Unassigned for any untagged part)."""
    f = spec["lines"]
    include = _code_test(f["account_codes"]) if f["account_codes"] else None
    exclude = _code_test(f["exclude_account_codes"]) if f["exclude_account_codes"] else None
    ratio_codes = spec["ratio"].get("numerator_codes", []) + spec["ratio"].get("denominator_codes", []) \
        if spec["measure"] == "ratio" else []
    in_ratio = _code_test(ratio_codes) if ratio_codes else None
    dimension = _dimension(organisation, f["dimension"]) if f["dimension"] else None
    tags = _tags(organisation, dimension) if dimension else {}
    names = dict(DimensionValue.objects.filter(dimension=dimension).values_list("pk", "name")) if dimension else {}
    out = []
    for pl_line in period_lines(organisation, start, end):
        code = pl_line.account.code or ""
        if f["categories"] and pl_line.category not in f["categories"]:
            continue
        if include and not include(code):
            continue
        if exclude and exclude(code):
            continue
        if in_ratio and not in_ratio(code):
            continue
        if f["types"] and _type(pl_line.parent) not in f["types"]:
            continue
        if f["contact_contains"] and not any(w in _contact(pl_line.parent).lower() for w in f["contact_contains"]):
            continue
        if f["text_contains"] and not any(w in _haystack(pl_line) for w in f["text_contains"]):
            continue
        if f["min_amount"] is not None and abs(pl_line.amount) < Decimal(str(f["min_amount"])):
            continue
        if dimension is None:
            out.append((pl_line, pl_line.amount, None))
            continue
        shares = tags.get((pl_line.line_field, pl_line.line.pk)) or []
        rest = 1 - sum((a for _, a in shares), ZERO)
        if rest > 0:
            shares = shares + [(UNASSIGNED, rest)]
        for (value_id, _), part in zip(shares, split_amount(pl_line.amount, [a for _, a in shares])):
            if not part:
                continue
            assigned = value_id is not UNASSIGNED
            wanted = f["dimension_filter"]
            if wanted == "unassigned" and assigned or wanted == "assigned" and not assigned:
                continue
            out.append((pl_line, part, names.get(value_id, "Unassigned") if assigned else "Unassigned"))
    return out


def run_spec(organisation, spec, months):
    """The check's result over ``months`` (Periods): {"columns": [[label, kind]], "rows": [[...]], "count",
    "total"}. Several months add a Month column first, so a trial run reads month by month."""
    many = len(months) > 1
    columns, rows = None, []
    for month in months:
        cols, month_rows = _run_month(organisation, spec, month)
        columns = ([["Month", "text"]] if many else []) + cols
        rows += [([month.start.strftime("%b %Y")] if many else []) + r for r in month_rows]
    amount_at = next((i for i, (label, kind) in enumerate(columns or []) if label == "Amount"), None)
    total = sum((Decimal(r[amount_at]) for r in rows if amount_at is not None and r[amount_at] not in (None, "")),
                ZERO)
    return {"columns": columns or [], "rows": rows, "count": len(rows), "total": str(total)}


def _run_month(organisation, spec, month):
    entries = _lines(organisation, spec, month.start, month.end)
    group_by, raise_when = spec["group_by"], spec["raise_when"]
    over = raise_when.get("amount_over")
    if group_by == "line":
        columns = [["Date", "text"], ["Type", "text"], ["Number", "text"], ["Contact", "text"], ["Account", "text"],
                   ["Description", "text"]]
        if spec["lines"]["dimension"]:
            columns.append([spec["lines"]["dimension"], "text"])
        columns.append(["Amount", "money"])
        rows = []
        for pl_line, amount, value in sorted(entries, key=lambda e: -abs(e[1])):
            if over is not None and abs(amount) <= Decimal(str(over)):
                continue
            row = [f"{pl_line.parent.date:%d %b}" if pl_line.parent.date else "", TYPE_LABEL[_type(pl_line.parent)],
                   _number_of(pl_line.parent), _contact(pl_line.parent),
                   f"{pl_line.account.code} {pl_line.account.name}".strip(), (pl_line.line.description or "")[:80]]
            if spec["lines"]["dimension"]:
                row.append(value)
            rows.append(row + [str(amount)])
        return columns, rows[:spec["limit"]]
    if group_by == "transaction":
        totals, first = defaultdict(lambda: ZERO), {}
        for pl_line, amount, _ in entries:
            key = (pl_line.line_field, pl_line.parent.pk)
            totals[key] += amount
            first.setdefault(key, pl_line)
        columns = [["Date", "text"], ["Type", "text"], ["Number", "text"], ["Contact", "text"], ["Amount", "money"]]
        rows = []
        for key, amount in sorted(totals.items(), key=lambda kv: -abs(kv[1])):
            if over is not None and abs(amount) <= Decimal(str(over)):
                continue
            parent = first[key].parent
            rows.append([f"{parent.date:%d %b}" if parent.date else "", TYPE_LABEL[_type(parent)], _number_of(parent),
                         _contact(parent), str(amount)])
        return columns, rows[:spec["limit"]]
    return _grouped(organisation, spec, month, entries)


def _group_key(spec, pl_line, value):
    group_by = spec["group_by"]
    if group_by == "contact":
        return _contact(pl_line.parent) or "(no contact)"
    if group_by == "account":
        return f"{pl_line.account.code} {pl_line.account.name}".strip()
    if group_by == "dimension":
        return value or "Unassigned"
    return f"{pl_line.parent.date:%b %Y}" if pl_line.parent.date else ""


def _sums(organisation, spec, entries):
    """{group: [lines, amount, numerator, denominator]}."""
    numerator = _code_test(spec["ratio"].get("numerator_codes", [])) if spec["measure"] == "ratio" else None
    denominator = _code_test(spec["ratio"].get("denominator_codes", [])) if spec["measure"] == "ratio" else None
    groups = defaultdict(lambda: [0, ZERO, ZERO, ZERO])
    for pl_line, amount, value in entries:
        g = groups[_group_key(spec, pl_line, value)]
        g[0] += 1
        g[1] += amount
        code = pl_line.account.code or ""
        if numerator and numerator(code):
            g[2] += amount
        if denominator and denominator(code):
            g[3] += amount
    return groups


def _grouped(organisation, spec, month, entries):
    label = {"contact": "Contact", "account": "Account", "dimension": spec["lines"]["dimension"] or "Value",
             "month": "Month"}[spec["group_by"]]
    groups = _sums(organisation, spec, entries)
    compared = {}
    if spec["compare"] != "none":
        start = add_months(month.start, -1 if spec["compare"] == "last_month" else -12)
        other = month_period(start.year, start.month)
        compared = _sums(organisation, spec, _lines(organisation, spec, other.start, other.end))
    rw, ratio = spec["raise_when"], spec["ratio"]
    columns = [[label, "text"], ["Lines", "number"]]
    if spec["measure"] == "ratio":  # the ratio's two sides, not their sum, are what matter
        columns += [[ratio["numerator_label"], "money"], [ratio["denominator_label"], "money"], ["Ratio", "pct"]]
    else:
        columns.append(["Amount", "money"])
    if compared or spec["compare"] != "none":
        columns += [["Last month" if spec["compare"] == "last_month" else "Last year", "money"], ["Change", "money"]]
    rows = []
    for key, (count, amount, num, den) in sorted(groups.items(), key=lambda kv: -abs(kv[1][1])):
        before = compared.get(key, [0, ZERO])[1] if spec["compare"] != "none" else None
        change = amount - before if before is not None else None
        if rw.get("amount_over") is not None and abs(amount) <= Decimal(str(rw["amount_over"])):
            continue
        if before is not None:
            if rw.get("new_only") and before:
                continue
            if rw.get("change_over") is not None and abs(change) <= Decimal(str(rw["change_over"])):
                continue
        row = [key, count]
        if spec["measure"] == "ratio":
            share = (num / den) if den else None
            pct = share * 100 if share is not None else None
            if ratio.get("below") is not None or ratio.get("above") is not None:
                low_hit = ratio.get("below") is not None and (pct is None or pct < Decimal(str(ratio["below"])))
                high_hit = ratio.get("above") is not None and pct is not None and pct > Decimal(str(ratio["above"]))
                if not (low_hit or high_hit):
                    continue
            row += [str(num), str(den), str(share.quantize(Decimal("0.0001"))) if share is not None else None]
        else:
            row.append(str(amount))
        if before is not None:
            row += [str(before), str(change)]
        rows.append(row)
    return columns, rows[:spec["limit"]]


# --- Trial run, confirm, and /review ------------------------------------------------------------------------------

def trial_months(request, today):
    settings = ReviewSettings.for_organisation(request.organisation)
    count = request.trial_months or settings.trial_months
    last = today.replace(day=1) - timedelta(days=1)
    firsts = [add_months(last.replace(day=1), -i) for i in range(count - 1, -1, -1)]
    return [month_period(d.year, d.month) for d in firsts]


def trial_run(request, user, *, source_factory=None, today=None):
    """Start the trial run in the background: copy the months from Xero, work out the check from the prompt, run
    it. Returns False if one is already running."""
    now = timezone.now()
    started = request.job_started_at
    if request.job_status == jobs.RUNNING and started and now - started < jobs.STALE_AFTER:
        return False
    ReviewRequest.objects.filter(pk=request.pk).update(job_status=jobs.RUNNING, job_message="Reading Xero…",
                                                       job_started_at=now)
    request_id, user_id = request.pk, getattr(user, "pk", None)

    def work():
        from django.contrib.auth import get_user_model

        row = ReviewRequest.objects.select_related("organisation").get(pk=request_id)
        try:
            if source_factory is None:
                from xero.source import source_for as factory
            else:
                factory = source_factory
            src = factory(row.organisation)
            day = today or timezone.localdate()
            months = trial_months(row, day)
            copy_period(row.organisation, src, KINDS, months[0].start, months[-1].end)
            ReviewRequest.objects.filter(pk=request_id).update(job_message="Working out the check…")
            spec = clean(compile_request(row.organisation, row.prompt, src.accounts()), row.organisation)
            result = run_spec(row.organisation, spec, months)
            who = get_user_model().objects.filter(pk=user_id).first()
            row.spec, row.plain, row.cannot = spec, spec["plain"], spec["cannot"]
            if not row.title:
                row.title = spec["title"]
            row.trial_result = result | {"months": [m.start.strftime("%b %Y") for m in months]}
            row.trial_prompt, row.trial_at, row.trial_by = row.prompt, timezone.now(), who
            row.status = RequestStatus.TRIAL
            row.log(who, "trial run", prompt=row.prompt, spec=spec, rows=result["count"])
            row.job_status, row.job_message = jobs.DONE, f"Trial run done: {result['count']} row(s)."
            row.save()
        except (LLMError, SpecError) as exc:
            ReviewRequest.objects.filter(pk=request_id).update(job_status=jobs.FAILED, job_message=str(exc)[:500])
        except Exception:  # noqa: BLE001 (shown on the admin page; details in the log)
            import logging

            logging.getLogger(__name__).exception("Review request trial run failed")
            ReviewRequest.objects.filter(pk=request_id).update(
                job_status=jobs.FAILED, job_message="The trial run failed; it has been logged. Try again.")

    jobs.start(work)
    return True


def confirm(request, user):
    """Freeze the latest trial run's check: /review runs it from now on."""
    if not request.spec or request.status != RequestStatus.TRIAL:
        raise SpecError("Trial run it first, then confirm.")
    if request.trial_prompt != request.prompt:
        raise SpecError("The request has changed since its trial run: trial run it again, then confirm.")
    request.confirmed_spec, request.confirmed_plain = request.spec, request.plain
    request.confirmed_at, request.confirmed_by = timezone.now(), user
    request.status = RequestStatus.CONFIRMED
    request.log(user, "confirmed", prompt=request.prompt, spec=request.spec)
    request.save()


def run_confirmed(organisation, src, month, fy, notes):
    """The result of every confirmed review request that's on, in number order, as dicts (code, title, columns,
    rows, count, total). The month's transactions are copied from Xero first (once)."""
    requests = [r for r in ReviewRequest.objects.filter(organisation=organisation) if r.runs]
    if not requests:
        return []
    copy_period(organisation, src, KINDS, month.start, month.end)
    found = []
    for r in requests:
        try:
            result = run_spec(organisation, r.confirmed_spec, [month])
        except (SpecError, KeyError) as exc:
            notes.append(f"{r.code} couldn't run: {exc}. Trial run it again in Admin > Review.")
            continue
        found.append({"code": r.code, "title": r.title or r.confirmed_spec.get("title", ""),
                      "columns": result["columns"], "rows": result["rows"], "count": result["count"],
                      "total": str(result["total"] or 0)})
    return found


def describe(spec):
    """The check's building blocks in short plain words, for admin."""
    f = spec.get("lines", {})
    parts = []
    if f.get("categories"):
        parts.append("categories " + ", ".join(f["categories"]))
    if f.get("account_codes"):
        parts.append("accounts " + ", ".join(f["account_codes"]))
    if f.get("exclude_account_codes"):
        parts.append("not " + ", ".join(f["exclude_account_codes"]))
    if f.get("types"):
        parts.append("types " + ", ".join(TYPE_LABEL[t] for t in f["types"]))
    if f.get("dimension"):
        parts.append(f"{f['dimension']} {f.get('dimension_filter', 'any')}")
    if f.get("contact_contains"):
        parts.append("contact has " + " / ".join(f["contact_contains"]))
    if f.get("text_contains"):
        parts.append("text has " + " / ".join(f["text_contains"]))
    parts.append(f"by {spec.get('group_by')}")
    if spec.get("compare", "none") != "none":
        parts.append(f"vs {spec['compare'].replace('_', ' ')}")
    rw = spec.get("raise_when", {})
    if rw.get("amount_over") is not None:
        parts.append(f"over ${rw['amount_over']:,.0f}")
    if rw.get("change_over") is not None:
        parts.append(f"change over ${rw['change_over']:,.0f}")
    if rw.get("new_only"):
        parts.append("new only")
    return re.sub(r"\s+", " ", " · ".join(parts))
