"""/tag: tag transactions to a value of an iLA dimension, e.g. bills and invoices to events.

``/tag --dim event --bills --invoices --Mar 2026 to Apr 2026 [filters] [--to <value>]``

1. The chosen transaction types for the period (both end dates included) are copied from Xero (xero.sync).
2. Transactions are kept when they have P&L lines (or lines on the --account given) and pass every filter.
   Transactions already tagged in the dimension are left out unless --retag.
3. With --to, every one goes to that value. Otherwise iLA proposes a value per transaction from the evidence, with
   a confidence and a reason (see ``_propose``).
4. The preview waits for the user: approve all, by confidence or by row number; skip rows; set a row's value. Approved
   transactions are tagged as a whole (transaction level), confirmed and marked as decided by the user. Xero is never
   changed.
"""

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from core import tagging
from core.models import (
    Account,
    AuditLog,
    BankTransaction,
    Dimension,
    DimensionValue,
    Document,
    Journal,
    Tag,
    TrackingCategory,
)
from xero.sync import copy_period

from .models import TagRun
from .output import DATE, MONEY, NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from .registry import CommandError, positive_int, read_params

KINDS = ("invoices", "bills", "credits", "bank", "journals")
KIND_WORDS = {"invoices": "invoices", "bills": "bills", "credits": "credit notes", "bank": "bank transactions",
              "journals": "manual journals"}
P_AND_L = (Account.AccountClass.REVENUE, Account.AccountClass.EXPENSE)
HIGH, MEDIUM, LOW = Tag.Confidence.HIGH, Tag.Confidence.MEDIUM, Tag.Confidence.LOW
RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}
DEFAULT_WINDOW = 3
MAX_SHOWN = 500
CANCEL_WORDS = {"cancel", "/cancel", "stop", "quit"}
INSTRUCTIONS = ("Type approve to tag every row with a proposal, approve high (or approve high medium) by confidence, "
                "approve 1-5,8 for chosen rows; skip 3,4 to leave rows out; set 3,4 <code> to choose the value "
                "yourself; or cancel. Nothing is tagged until you approve.")


# --- Entry points -----------------------------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return TagRun.objects.filter(organisation=ctx.organisation, user=ctx.user, status=TagRun.Status.OPEN).first()


def cancel(ctx):
    run = open_draft(ctx)
    if run is None:
        return None
    run.status = TagRun.Status.CANCELLED
    run.save()
    return Result("Tagging cancelled", subtitle="Nothing was tagged.")


def _text(value, ctx):
    return value.strip()


def _amount(value, ctx):
    """">1000", "<500", "500 to 5000" or "1200" -> (low, high), either may be None."""
    text = value.replace(",", "").replace("$", "").strip()
    try:
        if " to " in text:
            low, high = (Decimal(part.strip()) for part in text.split(" to ", 1))
            return (min(low, high), max(low, high))
        if text[:2] in (">=", "<="):
            number = Decimal(text[2:].strip())
            return (number, None) if text[0] == ">" else (None, number)
        if text[:1] in "<>":
            number = Decimal(text[1:].strip())
            return (number, None) if text[0] == ">" else (None, number)
        number = Decimal(text)
        return (number, number)
    except ArithmeticError:
        raise CommandError(f'--amount "{value}" isn\'t an amount. Try --amount >1000 or --amount 500 to 5000.') from None


def start(ctx, cmd):
    if ctx.organisation is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(
        cmd, ctx, flags=KINDS + ("all", "attachment", "retag"), period=True,
        keyed={"dim": _text, "desc": _text, "contact": _text, "account": _text, "amount": _amount, "xero": _text,
               "to": _text, "window": positive_int})
    if cmd.text:
        raise CommandError(f'Put every option after "--", e.g. /tag --dim event --bills --last month '
                           f'--desc {cmd.text}.')
    dimension = _dimension(ctx, params.values.get("dim"))
    kinds = list(KINDS) if "all" in params.flags else [k for k in KINDS if k in params.flags]
    if not kinds:
        raise CommandError("Say what to tag: --invoices, --bills, --credits, --bank, --journals (any of them), or "
                           "--all.")
    if params.period is None:
        raise CommandError("Give the period, e.g. --Mar 2026, --last month or --Jul 2026 to Sep 2026. Both end "
                           "dates are included.")
    period = params.period
    target = _value(dimension, params.values["to"]) if "to" in params.values else None

    copied = copy_period(ctx.organisation, ctx.source, kinds, period.start, period.end)
    xero = _xero_filter(ctx, params.values["xero"]) if "xero" in params.values else None
    found = _candidates(ctx, dimension, kinds, period, params, xero)
    window = params.values.get("window", DEFAULT_WINDOW)
    retag = "retag" in params.flags
    rows = [_row(txn, lines, dimension, target, retag) for txn, lines in found]
    if target is None:
        _propose_all(ctx, dimension, found, rows, window, retag)

    TagRun.objects.filter(organisation=ctx.organisation, user=ctx.user, status=TagRun.Status.OPEN).update(
        status=TagRun.Status.CANCELLED)
    run = TagRun.objects.create(organisation=ctx.organisation, user=ctx.user, dimension=dimension, data={
        "command": cmd.raw, "period": [period.start.isoformat(), period.end.isoformat()], "kinds": kinds,
        "to": target.pk if target else None, "retag": retag, "rows": rows,
        "copied": {"added": copied.added, "updated": copied.updated, "unchanged": copied.unchanged,
                   "skipped_lines": len(copied.skipped_lines)},
    })
    return show(ctx, run)


def answer(ctx, run, text, files=()):
    text = (text or "").strip()
    low = text.lower()
    if low in CANCEL_WORDS:
        return cancel(ctx)
    rows = run.data["rows"]
    if low.startswith("approve"):
        rest = low[len("approve"):].strip()
        if not rest or rest in ("all", "all proposals"):
            chosen = [r for r in rows if r["value"]]
        elif set(rest.replace(",", " ").split()) <= set(RANK):
            levels = set(rest.replace(",", " ").split())
            chosen = [r for r in rows if r["value"] and r["confidence"] in levels]
        else:
            numbers = _numbers(rest, len(rows))
            if numbers is None:
                return show(ctx, run, intro=f'"{text}" isn\'t something I can approve. {INSTRUCTIONS}')
            chosen = [rows[n - 1] for n in numbers]
            missing = [str(n) for n in numbers if not rows[n - 1]["value"]]
            if missing:
                return show(ctx, run, intro=f"Row(s) {', '.join(missing)} have no value yet. Choose one with set "
                                            f"{','.join(missing)} <code>, or leave them out.")
        if not chosen:
            return show(ctx, run, intro="Nothing to approve: no row has a proposal at that confidence.")
        return _approve(ctx, run, chosen)
    match = re.match(r"(skip|set)\s+([\d,\s-]+?)(?:\s+(.+))?$", low)
    if match:
        numbers = _numbers(match.group(2), len(rows))
        if numbers is None:
            return show(ctx, run, intro=f"Use row numbers from the # column, e.g. {match.group(1)} 3,5-7.")
        if match.group(1) == "skip":
            for n in numbers:
                rows[n - 1].update(value=None, code="", name="", confidence="", reason="Skipped by you")
            run.save()
            return show(ctx, run, intro=f"Left out row(s) {_ranges(numbers)}.")
        if not match.group(3):
            return show(ctx, run, intro="Say which value, e.g. set 3,4 260328ABB.")
        try:
            value = _value(run.dimension, text[match.end(2):].strip())
        except CommandError as exc:
            return show(ctx, run, intro=str(exc))
        for n in numbers:
            rows[n - 1].update(value=value.pk, code=value.code, name=value.name, confidence=HIGH,
                               reason="Chosen by you")
        run.save()
        return show(ctx, run, intro=f"Row(s) {_ranges(numbers)} set to {value.code} {value.name}.")
    return show(ctx, run, intro=INSTRUCTIONS)


# --- Reading the command ----------------------------------------------------------------------------------------

def _dimension(ctx, name):
    active = list(Dimension.objects.filter(organisation=ctx.organisation, is_active=True).order_by("name"))
    if not active:
        raise CommandError("There's nothing to tag to yet. Set up a dimension first with /createTag.")
    if name is None:
        if len(active) == 1:
            return active[0]
        raise CommandError("Say which dimension with --dim: " + ", ".join(d.name for d in active) + ".")
    wanted = name.strip().lower()
    found = [d for d in active if wanted in (d.code.lower(), d.name.lower())]
    if not found:
        raise CommandError(f'No iLA dimension called "{name}". Yours: ' + ", ".join(d.name for d in active)
                           + ". (Xero's tracking is used with --xero; /tag always tags iLA dimensions.)")
    return found[0]


def _value(dimension, text):
    wanted = text.strip().lower()
    values = list(dimension.values.filter(is_active=True))
    exact = [v for v in values if wanted in (v.code.lower(), v.name.lower())]
    if len(exact) == 1:
        return exact[0]
    partial = exact or [v for v in values if wanted in v.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise CommandError(f'No {dimension.name} value matches "{text}". Type /dim {dimension.name} to see them.')
    raise CommandError(f'"{text}" matches several {dimension.name} values: '
                       + ", ".join(f"{v.code} {v.name}" for v in partial[:6]) + ". Use the code.")


def _xero_filter(ctx, text):
    """``--xero Event`` or ``--xero Event 260328ABB``: (category, option name or ""). Categories are iLA's copy
    of Xero's, refreshed by the copy that runs first."""
    categories = sorted(TrackingCategory.objects.filter(organisation=ctx.organisation), key=lambda c: -len(c.name))
    for c in categories:
        if text.lower() == c.name.lower() or text.lower().startswith(c.name.lower() + " "):
            return c, text[len(c.name):].strip()
    names = ", ".join(sorted(c.name for c in categories)) or "none"
    raise CommandError(f"--xero needs one of Xero's tracking categories ({names}), e.g. --xero Event or "
                       "--xero Event 260328ABB.")


def _numbers(text, count):
    numbers = []
    for part in [p for p in re.split(r"[,\s]+", text.strip()) if p]:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= first <= last <= count:
            return None
        numbers += range(first, last + 1)
    return sorted(set(numbers)) or None


def _ranges(numbers):
    return ", ".join(str(n) for n in numbers)


# --- Finding transactions ---------------------------------------------------------------------------------------

def _candidates(ctx, dimension, kinds, period, params, xero):
    """[(transaction, relevant lines)] passing every filter, already tagged ones included."""
    org, start, end = ctx.organisation, period.start, period.end
    querysets = []
    doc_types = [t for k, t in (("invoices", "invoice"), ("bills", "bill"), ("credits", "credit_note")) if k in kinds]
    if doc_types:
        querysets.append(Document.objects.filter(
            organisation=org, type__in=doc_types, date__gte=start, date__lte=end,
            status__in=(Document.Status.AUTHORISED, Document.Status.PAID)).select_related("contact"))
    if "bank" in kinds:
        querysets.append(BankTransaction.objects.filter(
            organisation=org, date__gte=start, date__lte=end, status=BankTransaction.Status.AUTHORISED)
            .select_related("contact"))
    if "journals" in kinds:
        querysets.append(Journal.objects.filter(organisation=org, date__gte=start, date__lte=end,
                                                status=Journal.Status.POSTED))
    desc = _alternatives(params.values.get("desc"))
    contacts = _alternatives(params.values.get("contact"))
    accounts = _alternatives(params.values.get("account"))
    amount = params.values.get("amount")
    found = []
    for qs in querysets:
        for txn in qs.prefetch_related("lines__account", "lines__tracking__category", "lines__tags").order_by("date", "pk"):
            lines = [line for line in txn.lines.all() if line.account is not None and (
                any(_account_matches(line.account, a) for a in accounts) if accounts
                else line.account.account_class in P_AND_L)]
            if not lines:
                continue
            if desc and not any(term in _haystack(txn) for term in desc):
                continue
            contact = getattr(txn, "contact", None)
            if contacts and not (contact and any(term in contact.name.lower() for term in contacts)):
                continue
            if amount and not _in_range(_amount_of(txn, lines), amount):
                continue
            if "attachment" in params.flags and not txn.has_attachments:
                continue
            if xero and not any(o.category_id == xero[0].pk and (not xero[1] or o.name.lower() == xero[1].lower())
                                for line in lines for o in line.tracking.all()):
                continue
            found.append((txn, lines))
    return found


def _alternatives(text):
    """"ABBA or Bjorn" -> ["abba", "bjorn"]."""
    if not text:
        return []
    return [t.strip().lower() for t in re.split(r"\s+or\s+", text) if t.strip()]


def _account_matches(account, term):
    return account.code.lower() == term or term in account.name.lower()


def _haystack(txn):
    parts = [getattr(txn, "number", ""), getattr(txn, "reference", ""), getattr(txn, "narration", "")]
    parts += [line.description for line in txn.lines.all()]
    return " ".join(p for p in parts if p).lower()


def _amount_of(txn, lines):
    """The P&L amount: what the relevant lines add up to (for a journal, its debits, so a reclass isn't zero)."""
    if isinstance(txn, Journal):
        return sum((line.amount for line in lines if line.amount > 0), Decimal("0"))
    return abs(sum((line.line_amount for line in lines), Decimal("0")))


def _in_range(value, bounds):
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


def _live_tags(line, dimension):
    return [t for t in line.tags.all() if t.dimension_id == dimension.pk and t.status not in Tag.NOT_LIVE]


def _kind(txn):
    if isinstance(txn, Document):
        return "document"
    return "bank" if isinstance(txn, BankTransaction) else "journal"


def _type_label(txn):
    if isinstance(txn, Document):
        if txn.type == Document.Type.CREDIT_NOTE:
            return "Sales credit" if txn.ledger == Document.Ledger.RECEIVABLE else "Supplier credit"
        return txn.get_type_display()
    if isinstance(txn, BankTransaction):
        return "Receive money" if txn.direction == BankTransaction.Direction.RECEIVE else "Spend money"
    return "Journal"


def _xero_tracking(lines, dimension):
    """Xero's tracking on the lines: the options of the dimension's own category when it replaces one (e.g.
    260910COW), otherwise every category as "Category: Option"."""
    found = set()
    for line in lines:
        for option in line.tracking.all():
            if dimension.tracking_category_id:
                if option.category_id == dimension.tracking_category_id:
                    found.add(option.name)
            else:
                found.add(f"{option.category.name}: {option.name}")
    return ", ".join(sorted(found))


def _row(txn, lines, dimension, target, retag):
    current = sorted({t.dimension_value_id for line in lines for t in _live_tags(line, dimension)})
    tagged = all(_live_tags(line, dimension) for line in lines)
    description = next((line.description for line in lines if line.description), "")
    row = {
        "key": f"{_kind(txn)}:{txn.pk}", "type": _type_label(txn), "date": txn.date.isoformat() if txn.date else "",
        "number": getattr(txn, "number", "") or getattr(txn, "reference", ""),
        "contact": txn.contact.name if getattr(txn, "contact", None) else "",
        "text": (getattr(txn, "narration", "") or description or getattr(txn, "reference", ""))[:120],
        "amount": str(_amount_of(txn, lines)), "current": current, "tagged": tagged,
        "xero": _xero_tracking(lines, dimension),
        "value": None, "code": "", "name": "", "confidence": "", "reason": "",
    }
    if tagged and not retag:
        row["reason"] = "Already tagged; set it to change, or add --retag for a proposal"
    elif target is not None:
        row.update(value=target.pk, code=target.code, name=target.name, confidence=HIGH, reason="Chosen with --to")
    return row


# --- Proposing values -------------------------------------------------------------------------------------------

def _date_keys(dimension):
    dates = [f for f in dimension.attribute_fields if f.get("type") == Dimension.AttributeType.DATE]
    if not dates:
        return None, None
    label = lambda f: (f.get("label") or f["key"]).lower()  # noqa: E731
    start = next((f["key"] for f in dates if "start" in label(f)), dates[0]["key"])
    end = next((f["key"] for f in dates if "end" in label(f) or "finish" in label(f)), start)
    return start, end


def _windows(values, dimension):
    start_key, end_key = _date_keys(dimension)
    windows = {}
    if start_key is None:
        return windows
    for v in values:
        first = v.attributes.get(start_key)
        if not first:
            continue
        last = v.attributes.get(end_key) or first
        try:
            windows[v.pk] = (date.fromisoformat(first), max(date.fromisoformat(first), date.fromisoformat(last)))
        except ValueError:
            continue
    return windows


def _word(text):
    return re.compile(r"(?<![a-z0-9])" + re.escape(text.lower()) + r"(?![a-z0-9])")


# Words too common in event names to point at one event on their own.
GENERIC_WORDS = {
    "the", "and", "for", "with", "from", "night", "nights", "party", "dinner", "gala", "show", "shows", "training",
    "conference", "seminar", "event", "events", "game", "games", "season", "launch", "formal", "graduation",
    "graduations", "presentation", "expo", "convention", "regional", "lunch", "sports", "sport", "department",
    "education", "school", "college", "university", "meeting", "concert", "festival", "awards", "dance", "live",
    "tour", "final", "series", "national", "annual", "group", "club", "house", "centre", "center", "townsville",
    "australia", "australian", "queensland", "north", "christmas", "workshop", "breakfast",
}


def _name_words(value):
    return {w for w in re.findall(r"[a-z0-9']+", value.name.lower()) if len(w) >= 4 and w not in GENERIC_WORDS
            and not w.isdigit()}


def _distinctive_words(values):
    """{value id: [word pattern, ...]}: words of a value's name that no differently named value shares, e.g.
    "mecca" for MECCA Training (two MECCA Training events share it, which is fine: the date decides)."""
    names_with = {}
    for v in values:
        for w in _name_words(v):
            names_with.setdefault(w, set()).add(v.name.lower())
    return {v.pk: [(w, _word(w)) for w in sorted(_name_words(v)) if len(names_with[w]) == 1] for v in values}


def _propose_all(ctx, dimension, found, rows, window, retag):
    values = list(dimension.values.filter(is_active=True))
    windows = _windows(values, dimension)
    by_option = {v.tracking_option_id: v for v in values if v.tracking_option_id}
    words = _distinctive_words(values)
    patterns = [(v, _word(v.code), _word(v.name) if len(v.name) >= 3 else None, words[v.pk]) for v in values]
    contact_values = _contact_history(ctx, dimension)
    for (txn, lines), row in zip(found, rows):
        if row["tagged"] and not retag:
            continue
        proposal = _propose(txn, lines, values, windows, window, by_option, patterns, contact_values)
        if proposal:
            value, confidence, reason = proposal
            if value is not None:
                row.update(value=value.pk, code=value.code, name=value.name)
            row.update(confidence=confidence if value else "", reason=reason)


def _contact_history(ctx, dimension):
    """{contact id: {value id, ...}} from live tags already in the dimension."""
    history = {}
    tags = (Tag.objects.filter(organisation=ctx.organisation, dimension=dimension)
            .exclude(status__in=Tag.NOT_LIVE)
            .values_list("dimension_value_id", "document_line__document__contact_id",
                         "bank_transaction_line__bank_transaction__contact_id"))
    for value_id, doc_contact, bank_contact in tags:
        contact = doc_contact or bank_contact
        if contact:
            history.setdefault(contact, set()).add(value_id)
    return history


def _propose(txn, lines, values, windows, window, by_option, patterns, contact_values):
    """(value or None, confidence, reason). Evidence, strongest first: Xero tracking; the value's code in the text;
    its name, or a distinctive word of it (e.g. "Mecca"), in the text, high when the date also fits; then, weakly,
    the same contact as other transactions of a value whose dates fit (suppliers often work on many events). The
    date alone never proposes a value. When values tie on text evidence, the nearest date wins."""
    in_window = set()
    if txn.date:
        pad = timedelta(days=window)
        in_window = {pk for pk, (first, last) in windows.items() if first - pad <= txn.date <= last + pad}
    evidence = {}

    def add(value, rank, reason):
        best = evidence.get(value.pk)
        if best is None or rank > best[0]:
            evidence[value.pk] = (rank, reason, value)

    tracked = {by_option[o.pk] for line in lines for o in line.tracking.all() if o.pk in by_option}
    for value in tracked:
        add(value, 5, f"Xero tracking {value.code}")
    text = _haystack(txn)
    for value, code_re, name_re, word_res in patterns:
        fits = value.pk in in_window
        if code_re.search(text):
            add(value, 4, f'Code {value.code} in the text')
        elif name_re is not None and name_re.search(text):
            add(value, 4 if fits else 3, f'"{value.name}" in the text' + (", and the date fits" if fits else ""))
        else:
            word = next((w for w, pattern in word_res if pattern.search(text)), None)
            if word:
                add(value, 4 if fits else 3,
                    f'"{word}" (from {value.name}) in the text' + (", and the date fits" if fits else ""))
    contact = getattr(txn, "contact_id", None)
    for pk in contact_values.get(contact, set()) & in_window:
        value = next(v for v in values if v.pk == pk)
        add(value, 1.5, "Only that the same contact is tagged to it and the date fits; suppliers often work on "
                        "many events")
    if not evidence:
        # The date alone is never enough: overhead bills (power, cleaning) fall inside some event's dates.
        fitting = [v.code for v in values if v.pk in in_window]
        if fitting:
            return None, "", (f"Only the date fits {', '.join(fitting[:3])}"
                              + (f" and {len(fitting) - 3} more" if len(fitting) > 3 else "")
                              + "; the date alone isn't enough")
        return None, "", "No evidence"

    ranked = sorted(evidence.values(), key=lambda e: -e[0])
    top = [e for e in ranked if e[0] == ranked[0][0]]
    if len(top) > 1 and txn.date and top[0][0] >= 3:
        top = _nearest(top, windows, txn.date)
    if len(top) > 1:
        return None, "", "Could be " + " or ".join(e[2].code for e in top[:3])
    rank, reason, value = top[0]
    confidence = HIGH if rank >= 4 else MEDIUM if rank >= 2 else LOW
    return value, confidence, reason


def _nearest(tied, windows, day):
    """The tied entry whose dates are nearest ``day`` (e.g. which of two MECCA Training events a bill mentioning
    "Mecca" belongs to), with that noted in its reason; all of them if none or several are nearest."""
    def distance(entry):
        first, last = windows[entry[2].pk]
        return 0 if first <= day <= last else min(abs((day - first).days), abs((day - last).days))

    dated = [e for e in tied if e[2].pk in windows]
    if not dated:
        return tied
    best = min(distance(e) for e in dated)
    nearest = [e for e in dated if distance(e) == best]
    if len(nearest) != 1:
        return tied
    rank, reason, value = nearest[0]
    first = windows[value.pk][0]
    return [(rank, f"{reason}; the nearest {value.name} ({first:%d %b})", value)]


# --- Approving --------------------------------------------------------------------------------------------------

MODELS = {"document": Document, "bank": BankTransaction, "journal": Journal}


def _approve(ctx, run, chosen):
    dimension = run.dimension
    values = {v.pk: v for v in DimensionValue.objects.filter(dimension=dimension,
                                                             pk__in={r["value"] for r in chosen})}
    batch = uuid.uuid4()
    tagged, problems = 0, []
    with transaction.atomic():
        for r in chosen:
            kind, pk = r["key"].split(":")
            txn = MODELS[kind].objects.filter(organisation=ctx.organisation, pk=pk).first()
            value = values.get(r["value"])
            if txn is None or value is None:
                problems.append(f"{r['type']} {r['number'] or r['date']} is no longer available and was left out.")
                continue
            tagging.assign(txn, dimension, [(value, Decimal("1"))], method=Tag.Method.MANUAL,
                           confidence=r["confidence"] or HIGH, status=Tag.Status.CONFIRMED, set_by=ctx.user,
                           rule=r["reason"][:100], batch=batch, note=f"/tag {run.pk}")
            tagged += 1
        run.status = TagRun.Status.APPROVED
        run.data["approved"] = [r["key"] for r in chosen]
        run.save()
        AuditLog.objects.create(organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
                                input=run.data.get("command", "/tag")[:2000], target_type="console.tagrun",
                                target_id=str(run.pk),
                                result={"dimension": dimension.name, "tagged": tagged, "batch": str(batch)})
    total = sum((Decimal(r["amount"]) for r in chosen), Decimal("0"))
    result = Result(f"Tagged {tagged} transaction(s) to {dimension.name}",
                    subtitle="Saved in iLA. Xero hasn't changed.")
    result.add(Figures([Figure("Transactions tagged", str(tagged), TEXT), Figure("Amount", total, MONEY)]))
    for p in problems:
        result.add(Note(p, "warning"))
    approved = {r["key"] for r in chosen}
    left = sum(1 for r in run.data["rows"] if r["key"] not in approved and not r.get("tagged"))
    if left:
        result.add(Note(f"{left} row(s) weren't approved and stay untagged. Run the same /tag again to review them."))
    return result


# --- Display ----------------------------------------------------------------------------------------------------

def show(ctx, run, *, intro=""):
    data, dimension = run.data, run.dimension
    first, last = (date.fromisoformat(d) for d in data["period"])
    result = Result(f"/tag · {dimension.name} · {first:%d %b %Y} to {last:%d %b %Y}",
                    subtitle="Both dates included · " + ", ".join(KIND_WORDS[k] for k in data["kinds"]))
    if intro:
        result.add(Note(intro))
    rows = data["rows"]
    copied = data.get("copied", {})
    result.add(Note(f"Read from Xero: {copied.get('added', 0)} new, {copied.get('updated', 0)} changed and "
                    f"{copied.get('unchanged', 0)} unchanged transaction(s) in the period."
                    + (f" {copied['skipped_lines']} line(s) use an account iLA doesn't know and were left out."
                       if copied.get("skipped_lines") else "")))
    counts = {level: sum(1 for r in rows if r["value"] and r["confidence"] == level) for level in RANK}
    tagged = sum(1 for r in rows if r.get("tagged"))
    total = sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
    result.add(Figures([
        Figure("Found", f"{len(rows)}", TEXT, detail="Transactions with P&L lines passing your filters"),
        Figure("Amount", total, MONEY),
        Figure("Proposed", f"{counts[HIGH]} high · {counts[MEDIUM]} medium · {counts[LOW]} low", TEXT),
        Figure("No proposal", str(sum(1 for r in rows if not r["value"] and not r.get("tagged"))), TEXT,
               detail="Not tagged yet"),
        Figure("Already tagged", str(tagged), TEXT,
               detail="Shown in the table" + ("" if data.get("retag") else "; add --retag for proposals")),
    ]))
    if not rows:
        result.add(Note("Nothing found with these options. Widen the period or the filters."))
        return result
    current = {v.pk: v.code for v in DimensionValue.objects.filter(
        dimension=dimension, pk__in={pk for r in rows for pk in r["current"]})}
    table = result.add(Table([Column("#", NUMBER), Column("Type"), Column("Date", DATE), Column("Number"),
                              Column("Contact"), Column("Description"), Column("Amount", MONEY),
                              Column("Tagged in Xero"), Column("Tagged in iLA"),
                              Column(f"Tag to ({dimension.name})"), Column("Confidence"),
                              Column("Why")], title=f"Transactions ({len(rows)})"))
    for n, r in enumerate(rows[:MAX_SHOWN], 1):
        table.add(n, r["type"], date.fromisoformat(r["date"]) if r["date"] else None, r["number"], r["contact"],
                  r["text"], Decimal(r["amount"]), r.get("xero", ""),
                  ", ".join(current.get(pk, "") for pk in r["current"]),
                  f"{r['code']} {r['name']}".strip(), r["confidence"].title(), r["reason"])
    if len(rows) > MAX_SHOWN:
        result.add(Note(f"Showing the first {MAX_SHOWN} of {len(rows)}. Narrow the period or filters to see the "
                        "rest; approve still covers every row."))
    result.add(Note(INSTRUCTIONS, "warning"))
    return result
