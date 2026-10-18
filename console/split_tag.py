"""/splittag: split the amounts on chosen GL accounts across values of a dimension, e.g. September's labour
(6300-6380) across events, by fixed amounts, percentages or both.

``/splittag << labour.csv [--Sep 2026]``   read rules from a file (they're saved by name when approved)
``/splittag --labour --Sep 2026``          re-run a saved rule (also ``--rule labour``)
``/splittag --labour --Sep 2026 --gl 6355 --journals``   ... with its scope changed for this run only
``/splittag``                              list the saved rules

A rule has a scope (accounts, transaction types, optional contact and description filters, a period) and allocation
lines. Fixed amounts are taken first as a share of the scope's total; percentages apply to what's left; anything
still left stays untagged (Unassigned in reports). The same shares are applied to every line in the scope, so a
group of accounts is pro-rated. A line in two rules' scopes is an error. Splits are line-level, confirmed manual
tags, so they win over Xero tracking and over a whole-transaction /tag. Nothing changes in Xero.
"""

import csv
import io
import re
import uuid
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.urls import reverse

from core import tagging
from core.models import (
    AuditLog,
    BankTransaction,
    BankTransactionLine,
    Dimension,
    DimensionValue,
    Document,
    DocumentLine,
    JournalLine,
    Tag,
)
from xero.sync import KINDS, copy_period

from .models import SplitRule, SplitRun
from .output import MONEY, PCT, TEXT, Column, Figure, Figures, Note, Result, Table
from .periods import PeriodError, parse_date, parse_period
from .pl_by_dimension import period_lines
from .registry import CommandError, read_params

ZERO, ONE, HUNDRED = Decimal("0"), Decimal("1"), Decimal("100")
SHARE = Decimal("0.000000000001")
CENT = Decimal("0.01")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
RESERVED = set(KINDS) | {"all", "gl", "rule", "contact", "desc", "template", "new", "list", "by", "to"}
TYPE_WORDS = {"invoice": "invoices", "invoices": "invoices", "bill": "bills", "bills": "bills",
              "credit": "credits", "credits": "credits", "credit notes": "credits", "bank": "bank",
              "journal": "journals", "journals": "journals", "manual journals": "journals"}
CANCEL_WORDS = {"cancel", "/cancel", "stop", "quit"}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEMPLATE_ROWS = [
    ["Rule", "Accounts", "Types", "Contact", "Description", "From", "To", "Dimension", "Value", "Amount", "Percent"],
    ["labour", "6300-6380", "all", "", "", "01/09/2026", "30/09/2026", "Event", "260902MEC", "100", ""],
    ["labour", "6300-6380", "all", "", "", "01/09/2026", "30/09/2026", "Event", "260910COW", "300", ""],
    ["casuals", "6355", "journals", "", "", "01/09/2026", "30/09/2026", "Event", "260910COW", "", "30"],
    ["casuals", "6355", "journals", "", "", "01/09/2026", "30/09/2026", "Event", "260902MEC", "", "20"],
]


class RuleError(Exception):
    """A problem with a rule's definition: the messages are shown together."""


# --- Entry points -----------------------------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return SplitRun.objects.filter(organisation=ctx.organisation, user=ctx.user, status=SplitRun.Status.OPEN).first()


def cancel(ctx):
    run = open_draft(ctx)
    if run is None:
        return None
    run.status = SplitRun.Status.CANCELLED
    run.save()
    return Result("Split cancelled", subtitle="Nothing was tagged or saved.")


def answer(ctx, run, text, files=()):
    low = (text or "").strip().lower()
    if low in CANCEL_WORDS:
        return cancel(ctx)
    if low in ("approve", "approved", "ok approve", "yes approve"):
        return _approve(ctx, run)
    return show(run, intro="Type approve to save the split, or cancel. To change it, correct the file and run "
                           "/splittag again.")


def start(ctx, cmd):
    org = ctx.organisation
    if org is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    saved = {r.name.lower(): r for r in SplitRule.objects.filter(organisation=org).select_related("dimension")}
    params = read_params(cmd, ctx, flags=set(KINDS) | {"all", "template"} | (set(saved) - RESERVED), period=True,
                         keyed={"gl": _text, "rule": _text, "contact": _text, "desc": _text})
    if cmd.text:
        raise CommandError(f'Put every option after "--", e.g. /splittag --{cmd.text} --Sep 2026.')
    chosen = [f for f in params.flags if f in saved] or params.values.get("rule")
    if "template" in params.flags or (not cmd.input_file and not chosen):
        return _guide(saved, params.period)

    if cmd.input_file:
        rules = _rules_from_file(ctx, ctx.input_file)
        from_file = True
    else:
        names = [f for f in params.flags if f in saved]
        names += [n.strip().lower() for n in params.values.get("rule", "").split(",") if n.strip()]
        missing = [n for n in names if n not in saved]
        if missing:
            raise CommandError(f'No saved split rule called "{missing[0]}". ' + _saved_names(saved))
        rules = [_rule_from_saved(saved[n]) for n in dict.fromkeys(names)]
        from_file = False

    overrides = {}
    if "gl" in params.values:
        overrides["accounts"] = _accounts(params.values["gl"])
    kinds = list(KINDS) if "all" in params.flags else [k for k in KINDS if k in params.flags]
    if kinds:
        overrides["types"] = kinds
    for key in ("contact", "desc"):
        if key in params.values:
            overrides[key] = params.values[key].strip().lower()
    for rule in rules:
        rule["scope"].update(overrides)
        if params.period is not None:
            rule["period"] = [params.period.start.isoformat(), params.period.end.isoformat(), params.period.label]
        if not rule.get("period"):
            raise CommandError(f'Give the period for rule {rule["name"]}, e.g. /splittag --{rule["name"]} --Sep 2026 '
                               '(or fill in From and To in the file).')

    for start_, end_ in {(r["period"][0], r["period"][1]) for r in rules}:
        types = sorted({t for r in rules if (r["period"][0], r["period"][1]) == (start_, end_)
                        for t in r["scope"]["types"]})
        copy_period(org, ctx.source, types, date.fromisoformat(start_), date.fromisoformat(end_))

    problems = []
    for rule in rules:
        try:
            _compute(org, rule)
        except RuleError as exc:
            problems += [f"{rule['name']}: {m}" for m in exc.args]
    problems += _overlaps(rules)
    SplitRun.objects.filter(organisation=org, user=ctx.user, status=SplitRun.Status.OPEN).update(
        status=SplitRun.Status.CANCELLED)
    run = SplitRun(organisation=org, user=ctx.user, data={
        "command": cmd.raw, "rules": rules, "save_rules": from_file, "problems": problems})
    if not problems:
        run.save()
    else:
        run.status = SplitRun.Status.CANCELLED
    return show(run)


def _text(value, ctx):
    return value.strip()


def _saved_names(saved):
    if not saved:
        return "There are no saved rules yet."
    return "Saved rules: " + ", ".join(sorted(r.name for r in saved.values())) + "."


# --- Rules ------------------------------------------------------------------------------------------------------

def _accounts(text):
    """"6300-6380; 6400, 5000" -> [["6300", "6380"], ["6400", "6400"], ["5000", "5000"]]."""
    ranges = []
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        low, _, high = (p.strip() for p in part.partition("-")) if "-" in part else (part, "", part)
        if not low or not high:
            raise CommandError(f'"{part}" isn\'t an account or a range like 6300-6380.')
        ranges.append([low, high])
    if not ranges:
        raise CommandError("Give at least one account code, e.g. 6300-6380 or 6355.")
    return ranges


def _account_in(code, ranges):
    code = (code or "").strip()
    for low, high in ranges:
        if code.isdigit() and low.isdigit() and high.isdigit():
            if int(low) <= int(code) <= int(high):
                return True
        elif low.lower() <= code.lower() <= high.lower():
            return True
    return False


def _accounts_label(ranges):
    return "; ".join(low if low == high else f"{low}-{high}" for low, high in ranges)


def _types(text):
    text = (text or "").strip().lower()
    if text in ("", "all", "any"):
        return list(KINDS)
    found = []
    for part in re.split(r"[;,]|\s+and\s+", text):
        part = part.strip()
        if part and part not in TYPE_WORDS:
            raise CommandError(f'"{part}" isn\'t a type. Use invoices, bills, credits, bank, journals or all.')
        if part and TYPE_WORDS[part] not in found:
            found.append(TYPE_WORDS[part])
    return found


def _number(text, what):
    try:
        value = Decimal(text.replace(",", "").replace("$", "").replace("%", "").strip())
    except InvalidOperation:
        raise RuleError(f'{what} "{text}" isn\'t a number.') from None
    if value <= 0:
        raise RuleError(f"{what} {text} must be more than zero.")
    return value


def _rule_from_saved(rule):
    return {"name": rule.name, "dimension": rule.dimension_id,
            "scope": {"accounts": rule.scope["accounts"], "types": list(rule.scope.get("types") or KINDS),
                      "contact": rule.scope.get("contact", ""), "desc": rule.scope.get("desc", "")},
            "allocations": [dict(a) for a in rule.allocations], "period": None}


def _rules_from_file(ctx, upload):
    if not upload.name.lower().endswith(".csv"):
        raise CommandError(f"{upload.name} isn't a CSV file. Save it as CSV (in Excel: File → Save As → CSV UTF-8).")
    if upload.size > MAX_FILE_BYTES:
        raise CommandError(f"{upload.name} is over 2 MB.")
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    lines = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    if len(lines) < 2:
        raise CommandError(f"{upload.name} needs a heading row and at least one rule row.")
    index = {h.strip().lower(): i for i, h in enumerate(lines[0]) if h.strip()}
    for required in ("rule", "accounts", "value"):
        if required not in index:
            raise CommandError(f'{upload.name} needs a "{required.title()}" column. Download the template with '
                               "/splittag --template.")
    if "amount" not in index and "percent" not in index:
        raise CommandError(f'{upload.name} needs an "Amount" or a "Percent" column.')

    def cell(row, name):
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    dimensions = list(Dimension.objects.filter(organisation=ctx.organisation, is_active=True))
    rules, problems = {}, []
    for n, row in enumerate(lines[1:], 2):
        name = cell(row, "rule")
        try:
            if not NAME_RE.match(name) or name.lower() in RESERVED or _is_period(name, ctx):
                raise RuleError(f'"{name}" can\'t be a rule name. Use 2-40 letters, digits, "-" or "_", starting with '
                                "a letter, and not a word /splittag uses (e.g. bills, all, gl) or a period.")
            rule = rules.setdefault(name.lower(), {"name": name, "rows": [], "scope_cells": None, "dims": set()})
            scope_cells = tuple(cell(row, c) for c in ("accounts", "types", "contact", "description", "from", "to"))
            if rule["scope_cells"] is None:
                rule["scope_cells"] = scope_cells
            elif scope_cells != rule["scope_cells"]:
                raise RuleError(f"rule {name}'s Accounts, Types, Contact, Description, From and To must be the same "
                                f"on every row (row {n} differs).")
            dimension = _dimension(dimensions, cell(row, "dimension"))
            rule["dims"].add(dimension.pk)
            value = _value(dimension, cell(row, "value"))
            amount, percent = cell(row, "amount"), cell(row, "percent")
            if bool(amount) == bool(percent):
                raise RuleError("give either an Amount or a Percent.")
            rule["rows"].append({"value": value.pk, "code": value.code,
                                 "amount": str(_number(amount, "Amount")) if amount else None,
                                 "percent": str(_number(percent, "Percent")) if percent else None})
        except (RuleError, CommandError) as exc:
            problems.append(f"Row {n}: {exc}")
    result = []
    for rule in rules.values():
        accounts_text, types_text, contact, desc, first, last = rule["scope_cells"]
        try:
            if len(rule["dims"]) > 1:
                raise RuleError("every row of a rule must use the same dimension.")
            codes = [r["code"].lower() for r in rule["rows"]]
            if len(set(codes)) != len(codes):
                raise RuleError("a value appears twice; put it on one row.")
            percents = sum((Decimal(r["percent"]) for r in rule["rows"] if r["percent"]), ZERO)
            if percents > HUNDRED:
                raise RuleError(f"the percentages add up to {percents}%, more than 100%.")
            period = None
            if first or last:
                start_, end_ = parse_date(first), parse_date(last)
                if not start_ or not end_ or end_ < start_:
                    raise RuleError(f'From "{first}" and To "{last}" must be dates (30/09/2026), To on or after From.')
                period = [start_.isoformat(), end_.isoformat(), f"{start_:%d %b %Y} to {end_:%d %b %Y}"]
            result.append({"name": rule["name"], "dimension": next(iter(rule["dims"])),
                           "scope": {"accounts": _accounts(accounts_text), "types": _types(types_text),
                                     "contact": contact.lower(), "desc": desc.lower()},
                           "allocations": rule["rows"], "period": period})
        except (RuleError, CommandError) as exc:
            problems.append(f"Rule {rule['name']}: {exc}")
    if problems:
        raise CommandError(f"{upload.name} needs fixing: " + " ".join(problems[:12])
                           + (f" (and {len(problems) - 12} more)" if len(problems) > 12 else ""))
    return result


def _is_period(name, ctx):
    try:
        return parse_period(name, today=ctx.today, fy=ctx.fy) is not None
    except PeriodError:
        return False


def _dimension(dimensions, text):
    if not text:
        if len(dimensions) == 1:
            return dimensions[0]
        raise RuleError("say which Dimension (" + ", ".join(d.name for d in dimensions) + ").")
    found = [d for d in dimensions if text.lower() in (d.code.lower(), d.name.lower())]
    if not found:
        raise RuleError(f'there\'s no dimension called "{text}". Set it up with /createTag.')
    return found[0]


def _value(dimension, text):
    if not text:
        raise RuleError("Value is empty.")
    found = [v for v in dimension.values.filter(is_active=True) if text.lower() in (v.code.lower(), v.name.lower())]
    if len(found) != 1:
        raise RuleError(f'"{text}" isn\'t one {dimension.name} value (use its code; /dim {dimension.name} lists '
                        "them).")
    return found[0]


# --- Working out the shares -------------------------------------------------------------------------------------

KIND_OF = {"invoice": "invoices", "bill": "bills", "credit_note": "credits"}


def _kind(parent):
    if isinstance(parent, Document):
        return KIND_OF[parent.type]
    return "bank" if isinstance(parent, BankTransaction) else "journals"


def _text_of(pl_line):
    parent = pl_line.parent
    return " ".join(p for p in (pl_line.line.description, getattr(parent, "reference", ""),
                                getattr(parent, "number", ""), getattr(parent, "narration", "")) if p).lower()


def _compute(org, rule):
    """Find the rule's lines and work out each value's share. Fills rule["lines"], ["total"], ["shares"]."""
    scope = rule["scope"]
    start_, end_ = date.fromisoformat(rule["period"][0]), date.fromisoformat(rule["period"][1])
    lines = []
    for pl_line in period_lines(org, start_, end_, pl_only=False):
        if not _account_in(pl_line.account.code, scope["accounts"]):
            continue
        if _kind(pl_line.parent) not in scope["types"]:
            continue
        contact = getattr(pl_line.parent, "contact", None)
        if scope.get("contact") and not (contact and scope["contact"] in contact.name.lower()):
            continue
        if scope.get("desc") and scope["desc"] not in _text_of(pl_line):
            continue
        lines.append(pl_line)
    if not lines:
        raise RuleError(f"no lines on {_accounts_label(scope['accounts'])} ({', '.join(scope['types'])}) in "
                        f"{rule['period'][2]}.")
    total = sum((pl.amount for pl in lines), ZERO)
    fixed = [a for a in rule["allocations"] if a["amount"]]
    fixed_total = sum((Decimal(a["amount"]) for a in fixed), ZERO)
    if fixed and total <= 0:
        raise RuleError(f"the total in scope is {total:,.2f}, so fixed amounts can't be allocated.")
    if fixed_total > total:
        raise RuleError(f"the amounts add up to {fixed_total:,.2f}, more than the {total:,.2f} in scope.")
    shares, used = [], ZERO
    for a in fixed:
        share = (Decimal(a["amount"]) / total).quantize(SHARE, ROUND_DOWN)
        shares.append({"value": a["value"], "code": a["code"], "basis": f"{Decimal(a['amount']):,.2f}",
                       "share": str(share)})
        used += share
    left = ONE - used
    for a in rule["allocations"]:
        if a["percent"]:
            share = (Decimal(a["percent"]) / HUNDRED * left).quantize(SHARE, ROUND_DOWN)
            basis = f"{Decimal(a['percent']):g}%" + (" of what's left" if fixed else "")
            shares.append({"value": a["value"], "code": a["code"], "basis": basis, "share": str(share)})
    targets = _targets(total, rule["allocations"])
    rule["lines"] = _line_shares(lines, total, targets)
    rule["total"] = str(total)
    rule["shares"] = shares
    rule["targets"] = {str(k): str(v) for k, v in targets.items()}
    rule["replaces"] = _already_split(org, rule["dimension"], lines)


def _targets(total, allocations):
    """Each value's amount to the cent: fixed amounts exactly, then each percentage of what's left, rounded; never
    more than the total."""
    left = total - sum((Decimal(a["amount"]) for a in allocations if a["amount"]), ZERO)
    targets = {}
    for a in allocations:
        targets[a["value"]] = Decimal(a["amount"]) if a["amount"] else             (left * Decimal(a["percent"]) / HUNDRED).quantize(CENT, ROUND_HALF_UP)
    excess = sum(targets.values(), ZERO) - total
    if excess > 0 and allocations:
        targets[allocations[-1]["value"]] -= excess
    return targets


def _line_shares(lines, total, targets):
    """[[line field, line id, amount, {value id: share}]]: each value's amount spread over the lines in proportion
    to their amounts, to the cent, with the rounding left on the largest line, so the lines add up to exactly the
    value's amount (a fixed $100 is $100.00 in reports, not $99.98). Each line's share is its cents over its amount."""
    parts = {pl.line.pk: {} for pl in lines}
    largest = max(lines, key=lambda pl: abs(pl.amount))
    for value_id, target in targets.items():
        split = {pl.line.pk: (target * pl.amount / total).quantize(CENT, ROUND_HALF_UP) if total else ZERO
                 for pl in lines}
        split[largest.line.pk] += target - sum(split.values(), ZERO)
        for pk, part in split.items():
            parts[pk][value_id] = part
    result = []
    for pl in lines:
        shares = {}
        if pl.amount:
            for value_id, part in parts[pl.line.pk].items():
                share = (part / pl.amount).quantize(SHARE)
                if share > 0:
                    shares[str(value_id)] = share
            over = sum(shares.values(), ZERO) - ONE
            if over > 0:
                last = list(shares)[-1]
                shares[last] -= over
        result.append([pl.line_field, pl.line.pk, str(pl.amount), {k: str(v) for k, v in shares.items() if v > 0}])
    return result


def _already_split(org, dimension_id, lines):
    """How many of the lines already have a line-level manual tag in the dimension (this run replaces it)."""
    count = 0
    for pl in lines:
        if pl.line.assignments.filter(dimension_id=dimension_id, method=Tag.Method.MANUAL).exclude(
                status__in=Tag.NOT_LIVE).exists():
            count += 1
    return count


def _overlaps(rules):
    owner, problems = {}, []
    for rule in rules:
        for field, pk, *_ in rule.get("lines", []):
            other = owner.setdefault((field, pk), rule["name"])
            if other != rule["name"]:
                problems.append(f"{rule['name']}: a {field.replace('_', ' ')} is also in rule {other}'s scope. "
                                "Narrow one of them (accounts, types, contact or description).")
    return list(dict.fromkeys(problems))[:10]


# --- Approving --------------------------------------------------------------------------------------------------

LINE_MODELS = {"document_line": DocumentLine, "bank_transaction_line": BankTransactionLine,
               "journal_line": JournalLine}


def _approve(ctx, run):
    batch = uuid.uuid4()
    tagged = 0
    with transaction.atomic():
        for rule in run.data["rules"]:
            dimension = Dimension.objects.get(organisation=ctx.organisation, pk=rule["dimension"])
            values = {v.pk: v for v in DimensionValue.objects.filter(dimension=dimension)}
            for field, pk, _, line_shares in rule["lines"]:
                line = LINE_MODELS[field].objects.filter(organisation=ctx.organisation, pk=pk).first()
                allocations = [(values[int(v)], Decimal(share)) for v, share in line_shares.items()]
                if line is None or not allocations:
                    continue
                tagging.assign(line, dimension, allocations, method=Tag.Method.MANUAL, confidence=Tag.Confidence.HIGH,
                               status=Tag.Status.CONFIRMED, set_by=ctx.user, rule=f"split:{rule['name']}"[:100],
                               batch=batch, note=f"/splittag {run.pk} {rule['period'][2]}", partial=True)
                tagged += 1
            saved = SplitRule.objects.filter(organisation=ctx.organisation, name__iexact=rule["name"]).first()
            if run.data.get("save_rules"):
                saved = saved or SplitRule(organisation=ctx.organisation, created_by=ctx.user)
                saved.name, saved.dimension = rule["name"], dimension
                saved.scope = dict(rule["scope"])
                saved.allocations = rule["allocations"]
                saved.updated_by = ctx.user
            if saved is not None:
                saved.last_run = rule["period"][2]
                saved.save()
        run.status = SplitRun.Status.APPROVED
        run.save()
        AuditLog.objects.create(organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
                                input=run.data.get("command", "/splittag")[:2000], target_type="console.splitrun",
                                target_id=str(run.pk), result={"lines": tagged, "batch": str(batch),
                                                               "rules": [r["name"] for r in run.data["rules"]]})
    result = Result(f"Split saved: {tagged} line(s) tagged", subtitle="Saved in iLA. Xero hasn't changed.")
    names = ", ".join(r["name"] for r in run.data["rules"])
    if run.data.get("save_rules"):
        result.add(Note(f"Rule(s) saved: {names}. Run one again for another month with /splittag --"
                        f"{run.data['rules'][0]['name']} --Oct 2026."))
    result.add(Note("See the result with /pl --event for the same period."))
    return result


# --- Display ----------------------------------------------------------------------------------------------------

GUIDE_ROWS = (
    ("Rule", "A name, e.g. labour. Rows with the same name make one rule; it's saved so you can run it again."),
    ("Accounts", "6300-6380, 6355, or a list: 6300-6380; 6400."),
    ("Types", "all, or any of invoices, bills, credits, bank, journals (e.g. journals for only the journals)."),
    ("Contact, Description", "Optional: only lines whose contact or description contains this."),
    ("From, To", "Optional dates (30/09/2026). Leave empty to give the period in the command."),
    ("Dimension", "e.g. Event (can be empty if you have only one)."),
    ("Value", "The value's code, e.g. 260902MEC."),
    ("Amount or Percent", "One per row. Amounts come first; percentages apply to what's left; the rest stays "
                          "untagged."),
)


def _guide(saved, period):
    """The template to download, how to fill it in, the saved rules and the next command to type."""
    when = period.label if period else "Sep 2026"
    result = Result("Split rules", subtitle="Split GL amounts across events from a CSV of named rules")
    result.link, result.link_text = reverse("console:splittag_template"), "Download the template (splittag-template.csv)"
    table = result.add(Table([Column("Column"), Column("What goes in it")], title="Filling in the template"))
    for row in GUIDE_ROWS:
        table.add(*row)
    if saved:
        _saved_table(result, saved)
        first = sorted(saved.values(), key=lambda r: r.name.lower())[0].name
        result.add(Note(f"Next: run a saved rule with /splittag --{first} --{when}, or a new file with "
                        f"/splittag --{when} << rules.csv. Nothing is saved until you approve.", "warning"))
    else:
        result.add(Note(f"No saved rules yet. Download the template, fill it in (Excel is fine; save as CSV), then "
                        f"type /splittag --{when} << rules.csv. You'll see a preview; nothing is saved until you "
                        "approve.", "warning"))
    return result


def _saved_table(result, saved):
    table = result.add(Table([Column("Rule"), Column("Dimension"), Column("Accounts"), Column("Types"),
                              Column("Allocation"), Column("Last run")], title="Saved rules"))
    for rule in sorted(saved.values(), key=lambda r: r.name.lower()):
        allocation = ", ".join(f"{a['code']} {'$' + a['amount'] if a['amount'] else a['percent'] + '%'}"
                               for a in rule.allocations)
        types = rule.scope.get("types") or list(KINDS)
        table.add(rule.name, rule.dimension.name, _accounts_label(rule.scope["accounts"]),
                  "all" if set(types) == set(KINDS) else ", ".join(types), allocation, rule.last_run)


def show(run, *, intro=""):
    data = run.data
    rules = data["rules"]
    periods = sorted({r["period"][2] for r in rules if r.get("period")})
    result = Result("/splittag · " + " · ".join(periods), subtitle="Splits are tags in iLA; Xero isn't changed")
    if intro:
        result.add(Note(intro))
    if data.get("problems"):
        for p in data["problems"]:
            result.add(Note(p, "warning"))
        result.add(Note("Nothing was saved. Fix the rule (or the file) and run /splittag again."))
        return result
    lines = sum(len(r["lines"]) for r in rules)
    total = sum((Decimal(r["total"]) for r in rules), ZERO)
    allocated = sum((Decimal(v) for r in rules for v in r["targets"].values()), ZERO)
    result.add(Figures([
        Figure("Rules", str(len(rules)), TEXT), Figure("Lines", str(lines), TEXT),
        Figure("Total in scope", total, MONEY), Figure("Split", allocated.quantize(Decimal("0.01")), MONEY),
        Figure("Left untagged", (total - allocated).quantize(Decimal("0.01")), MONEY),
    ]))
    for rule in rules:
        scope = rule["scope"]
        label = (f"{rule['name']}: {_accounts_label(scope['accounts'])} · "
                 + ("all types" if set(scope["types"]) == set(KINDS) else ", ".join(scope["types"]))
                 + (f" · contact {scope['contact']}" if scope.get("contact") else "")
                 + (f" · description {scope['desc']}" if scope.get("desc") else "")
                 + f" · {len(rule['lines'])} line(s) · {rule['period'][2]}")
        table = result.add(Table([Column("Value"), Column("Basis"), Column("Share", PCT), Column("Amount", MONEY)],
                                 title=label))
        rule_total, used, targets = Decimal(rule["total"]), ZERO, rule.get("targets", {})
        names = {v.pk: v.name for v in DimensionValue.objects.filter(pk__in=[s["value"] for s in rule["shares"]])}
        for s in rule["shares"]:
            share = Decimal(s["share"])
            used += share
            name = names.get(s["value"], "")
            table.add(s["code"] if name in ("", s["code"]) else f"{s['code']} {name}", s["basis"], share,
                      Decimal(targets[str(s["value"])]))
        split = sum((Decimal(v) for v in targets.values()), ZERO)
        table.add("Unassigned", "What's left", ONE - used, rule_total - split)
        table.add("Total", "", ONE, rule_total, total=True)
        if rule.get("replaces"):
            result.add(Note(f"{rule['name']}: {rule['replaces']} of these line(s) already have a split or line tag "
                            "in this dimension; approving replaces it."))
    if data.get("save_rules"):
        result.add(Note("Approving also saves the rule(s) by name, so you can run them again for another period."))
    result.add(Note("Type approve to tag these lines with these shares, or cancel. Amounts per line are worked out "
                    "to the cent in reports.", "warning"))
    return result


def template_csv():
    out = io.StringIO()
    csv.writer(out).writerows(TEMPLATE_ROWS)
    return "﻿" + out.getvalue()

