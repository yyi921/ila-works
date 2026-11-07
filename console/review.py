"""/review: the month-end P&L review.

``/review [--Oct 2026] [--vs lm|ly|budget] [--mtd|--ytd] [--L0|--L1] [--rev ...]``
    L0 (the default): one table of the P&L by group (Revenue, Cost of sales, Gross profit, Labour, Other operating
    costs, EBITDA, …, Net profit) and the ratios (GP, COGS, labour, EBITDA and net profit % of revenue) against last
    month, the same period last year or budget, month or year to date; then every confirmed review request (RR-01…).
    L1 (--L1, or naming a group): the group's GL accounts, one table per group.

The review stays open, so plain input works it. Review points are referred to by account code (4061, 6300-6380),
group (rev, cogs, oth, lab, opx, da, int, tax) or request (RR-01), the same whatever the view or comparison:
    note 4061 <text> · signoff 4061 lab RR-01 · reopen lab · clear 4061 · notes [all]
    vs ly · ytd · mtd · L1 rev · L0 · refresh · done

A sign-off keeps the month's actual at the time; when a refresh shows a different amount the row turns amber
("changed since sign-off") until it's signed off again. No materiality and no commentary: numbers only. Figures stay
put while the review is open; refresh reads Xero again. Nothing changes in Xero.
"""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from review import checks as engine
from review.models import FindingStatus, ReviewFinding, ReviewSettings

from .models import ReviewSession
from .output import MONEY, PCT, TEXT, Column, Figure, Figures, Note, Points, Ratio, Result, Table
from .periods import month_period
from .registry import CommandError, read_params

ZERO = Decimal("0")
CLOSE_WORDS = {"cancel", "/cancel", "stop", "quit", "done", "close", "exit"}
HELP = ("Refer to a line by account code (4061, 6300-6380), group (rev, cogs, oth, lab, opx, da, int, tax) or RR-01. "
        "Type: note 4061 <text> · signoff 4061 lab RR-01 · reopen lab · clear 4061 · notes · vs lm|ly|budget · "
        "mtd · ytd · L1 rev · L0 · refresh · done.")
VS = {"lm": "lm", "last month": "lm", "ly": "ly", "last year": "ly", "same month last year": "ly",
      "budget": "bud", "bud": "bud"}
ALIASES = {key: key for key in engine.GROUPS} | {
    word: key for key, (_, words) in engine.GROUPS.items() for word in words}
SIGN_WORDS = ("sign off", "sign-off", "signoff")
TONE = {"signed": "ok", "changed": "warn", "open": ""}


# --- Dialogue plumbing (see dialogues.py) ------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return ReviewSession.objects.filter(organisation=ctx.organisation, user=ctx.user,
                                        status=ReviewSession.Status.OPEN).first()


def _close(ctx, keep=None):
    sessions = ReviewSession.objects.filter(organisation=ctx.organisation, user=ctx.user,
                                            status=ReviewSession.Status.OPEN)
    if keep is not None:
        sessions = sessions.exclude(pk=keep.pk)
    sessions.update(status=ReviewSession.Status.CLOSED)


def cancel(ctx):
    session = open_draft(ctx)
    if session is None:
        return None
    _close(ctx)
    return Result(f"Review closed: {session.period_end:%B %Y}",
                  subtitle=f"Notes and sign-offs are kept. Open it again with /review --{session.period_end:%b %Y}.")


def answer(ctx, session, text, files=()):
    raw = re.sub(r"\s+", " ", (text or "").strip())
    low = raw.lower()
    if low in CLOSE_WORDS:
        return cancel(ctx)
    if "lines" not in session.data or low == "refresh":
        session = _build(ctx, session.period_end, session)
        return show(ctx, session, intro="Figures read again from Xero; notes and sign-offs were kept.")
    view = dict(session.data["view"])
    intro = ""
    sign = next((w for w in SIGN_WORDS if low == w or low.startswith(w + " ")), None)
    word, _, rest = low.partition(" ")
    raw_rest = raw[len(word):].strip()
    if sign:
        intro = _sign_off(ctx, session, _refs(session, low[len(sign):]))
    elif word == "reopen":
        intro = _reopen(ctx, session, _refs(session, rest))
    elif word == "note":
        ref_text, _, note = raw_rest.partition(" ")
        if not note.strip():
            raise CommandError("Give the line and the note, e.g. note 4061 Cowboys final invoice slipped to Nov.")
        ref = _one_ref(session, ref_text)
        _add_note(ctx, session, ref, note.strip())
        intro = f"Note kept on {_ref_label(session, ref)}."
    elif word == "clear":
        ref = _one_ref(session, rest)
        point = _point(ctx, session, ref)
        point.log(ctx.user, "notes cleared", notes=point.note)
        point.note, point.updated_by = "", ctx.user
        point.save()
        intro = f"Notes cleared on {_ref_label(session, ref)}."
    elif word == "notes":
        return notes_view(ctx, session, every_month=rest.strip() in ("all", "--all"))
    elif word == "vs":
        view = _set_vs(view, VS.get(rest.strip()))
    elif low in ("mtd", "ytd"):
        view["ytd"] = low == "ytd"
        if view["ytd"] and view["vs"] == "lm":
            view["vs"] = "ly"
            intro = "Year to date is compared with last year or budget, not last month: now vs last year."
    elif low == "l0":
        view |= {"level": 0, "focus": []}
    elif word == "l1":
        view |= {"level": 1, "focus": _groups(rest)}
    elif low:
        raise CommandError(f'I didn\'t understand "{raw}". {HELP}')
    _check_view(view, session.data)
    session.data = session.data | {"view": view}
    session.save()
    return show(ctx, session, intro=intro)


# --- The command --------------------------------------------------------------------------------------------------

def start(ctx, cmd):
    org = ctx.organisation
    if org is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(cmd, ctx, flags={"refresh", "mtd", "ytd", "l0", "l1"} | set(ALIASES),
                         keyed={"vs": lambda value, _: _vs(value)}, period=True,
                         exclusive=[("mtd", "ytd"), ("l0", "l1")])
    if cmd.text:
        raise CommandError("Put every option after --, e.g. /review --Oct 2026 --vs budget.")
    period_end = _period_end(ctx, params.period)
    focus = list(dict.fromkeys(ALIASES[f] for f in params.flags if f in ALIASES))
    if "l0" in params.flags and focus:
        raise CommandError("Naming a group opens its GL accounts (L1); leave out --L0.")
    ytd = "ytd" in params.flags
    vs = params.values.get("vs") or ("ly" if ytd else "lm")
    if ytd and vs == "lm":
        raise CommandError("Year to date against last month doesn't mean anything: use --vs ly or --vs budget.")
    view = {"vs": vs, "ytd": ytd, "level": 1 if focus or "l1" in params.flags else 0, "focus": focus}
    session = open_draft(ctx)
    if session is not None and session.period_end != period_end:
        session = None
    if session is None or "lines" not in session.data or "refresh" in params.flags:
        session = _build(ctx, period_end, session)
    _close(ctx, keep=session)
    _check_view(view, session.data)
    session.data = session.data | {"view": view}
    session.save()
    return show(ctx, session)


def _vs(value):
    key = VS.get(re.sub(r"\s+", " ", value.strip().lower()))
    if key is None:
        raise CommandError(f'"{value}" isn\'t a comparison: use --vs lm (last month), ly (last year) or budget.')
    return key


def _set_vs(view, key):
    if key is None:
        raise CommandError("Compare with lm (last month), ly (last year) or budget, e.g. vs ly.")
    if key == "lm" and view["ytd"]:
        raise CommandError("Year to date against last month doesn't mean anything: type mtd first, or vs ly.")
    return view | {"vs": key}


def _check_view(view, data):
    if view["vs"] == "bud" and not data.get("budget"):
        raise CommandError("There's no budget in Xero for this month (or iLA has no permission to read it). "
                           "Compare with lm or ly.")


def _join_aliases(text):
    """Multi-word group names as one token: "cost of sales" -> "cogs"."""
    for word, key in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
        if " " in word:
            text = re.sub(rf"\b{re.escape(word)}\b", key, text)
    return text


def _groups(text):
    found = []
    for part in re.split(r"[,\s]+", _join_aliases(re.sub(r"\s+", " ", text.strip().lower()))):
        if not part:
            continue
        if part not in ALIASES:
            raise CommandError(f'"{part}" isn\'t a group. Use rev, cogs, oth, lab, opx, da, int or tax.')
        found.append(ALIASES[part])
    return list(dict.fromkeys(found))


def _period_end(ctx, period):
    if period is None:
        return ctx.today.replace(day=1) - timedelta(days=1)
    if period.start.day != 1 or (period.end + timedelta(days=1)).day != 1 or period.start.month != period.end.month:
        raise CommandError("/review looks at one whole month, e.g. /review --Oct 2026.")
    if period.end >= ctx.today:
        raise CommandError(f"{period.label} hasn't finished yet; /review looks at a full month.")
    return period.end


def _build(ctx, period_end, session=None):
    """Read the month's figures and review requests from Xero and keep them on the session."""
    org, src = ctx.organisation, ctx.source
    settings = ReviewSettings.for_organisation(org)
    month = month_period(period_end.year, period_end.month)
    lines, has_budget = engine.gather(src, settings, month, ctx.fy)
    notes = []
    from review.requests import run_confirmed

    requests = run_confirmed(org, src, month, ctx.fy, notes)
    view = (session.data.get("view") if session else None) or {"vs": "lm", "ytd": False, "level": 0, "focus": []}
    data = {"lines": [line.to_json() for line in lines], "budget": has_budget, "requests": requests,
            "notes": notes, "groups": engine.describe_groups(settings, src.accounts()),
            "built_at": timezone.now().isoformat(timespec="seconds"), "view": view}
    if session is None:
        return ReviewSession.objects.create(organisation=org, user=ctx.user, period_end=period_end, data=data)
    session.data = data
    session.save()
    return session


# --- References ---------------------------------------------------------------------------------------------------

def _lines(data):
    return [engine.Line.from_json(line) for line in data.get("lines", [])]


def _refs(session, text):
    """The references in text, in order: account codes, groups and requests. A range like 6300-6380 takes every
    account in the month's P&L with a code in it."""
    text = _join_aliases(re.sub(r"\s+", " ", text.strip().lower()))
    codes = [line.code for line in _lines(session.data) if line.code]
    requests = {r["code"] for r in session.data.get("requests", [])}
    found = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if part in ALIASES:
            found.append(ALIASES[part])
            continue
        m = re.fullmatch(r"rr-?0*(\d+)", part)
        if m:
            code = f"RR-{int(m.group(1)):02d}"
            if code not in requests:
                raise CommandError(f"{code} isn't a confirmed review request that's on.")
            found.append(code)
            continue
        if part in codes:
            found.append(part)
            continue
        hits = [c for c in codes if engine._codes(part)(c)] if "-" in part else []
        if not hits:
            raise CommandError(f'"{part}" isn\'t an account code, group or review request in '
                               f"{session.period_end:%b %Y}. {HELP}")
        found.extend(hits)
    if not found:
        raise CommandError("Say which lines, e.g. signoff 4061 lab RR-01.")
    return list(dict.fromkeys(found))


def _one_ref(session, text):
    refs = _refs(session, text)
    if len(refs) != 1:
        raise CommandError("A note goes on one line, e.g. note 4061 <text>.")
    return refs[0]


def _ref_label(session, ref):
    if ref in engine.GROUPS:
        return engine.GROUPS[ref][0]
    for r in session.data.get("requests", []):
        if r["code"] == ref:
            return f"{ref} {r['title']}"
    for line in _lines(session.data):
        if line.code == ref:
            return f"{line.code} {line.name}"
    return ref


def _actual(session, ref, lines):
    """The month's actual for a reference: what a sign-off is held against."""
    if ref in engine.GROUPS:
        return engine.totals(lines, "cur")[ref]
    for r in session.data.get("requests", []):
        if r["code"] == ref:
            return Decimal(r["total"])
    return next((line.values["cur"] for line in lines if line.code == ref), ZERO)


# --- Review points ------------------------------------------------------------------------------------------------

def _point(ctx, session, ref):
    point, _ = ReviewFinding.objects.get_or_create(
        organisation=ctx.organisation, period_end=session.period_end, key=ref,
        defaults={"subject": ref, "label": _ref_label(session, ref)[:255]})
    return point


def _points(org, period_end):
    return {p.key: p for p in ReviewFinding.objects.filter(organisation=org, period_end=period_end)
            .select_related("signed_by", "organisation")}


def _members(group, lines):
    return [line.code for line in lines if line.group == group and line.code]


def _sign_off(ctx, session, refs):
    lines = _lines(session.data)
    targets = []
    for ref in refs:
        targets.append(ref)
        if ref in engine.GROUPS:
            targets.extend(_members(ref, lines))
    now = timezone.now()
    for ref in dict.fromkeys(targets):
        point = _point(ctx, session, ref)
        point.status, point.signed_amount = FindingStatus.SIGNED_OFF, _actual(session, ref, lines)
        point.signed_at, point.signed_by, point.updated_by = now, ctx.user, ctx.user
        point.log(ctx.user, "signed off", amount=str(point.signed_amount))
        point.save()
    return f"Signed off: {', '.join(_ref_label(session, r) for r in refs)}."


def _reopen(ctx, session, refs):
    lines = _lines(session.data)
    targets = []
    for ref in refs:
        targets.append(ref)
        if ref in engine.GROUPS:
            targets.extend(_members(ref, lines))
        else:
            group = next((line.group for line in lines if line.code == ref), None)
            if group:
                targets.append(group)  # the group isn't wholly signed off any more
    points = _points(ctx.organisation, session.period_end)
    for ref in dict.fromkeys(targets):
        point = points.get(ref)
        if point is None or point.status != FindingStatus.SIGNED_OFF:
            continue
        point.status, point.updated_by = FindingStatus.OPEN, ctx.user
        point.log(ctx.user, "reopened", refs=", ".join(refs))
        point.save()
    return f"Reopened: {', '.join(_ref_label(session, r) for r in refs)}."


def _add_note(ctx, session, ref, text):
    point = _point(ctx, session, ref)
    today = ctx.today
    stamp = f"{getattr(ctx.user, 'username', '')} {today.day} {today:%b}"
    point.note = (point.note.rstrip() + "\n" if point.note.strip() else "") + f"{text} — {stamp}"
    point.updated_by = ctx.user
    point.log(ctx.user, "note", note=text)
    point.save()


def _local(org, when):
    """A stored time in the organisation's time zone."""
    return timezone.localtime(when, ZoneInfo(getattr(org, "timezone", None) or "Australia/Brisbane"))


def _moved(amount):
    return f"⚠ Changed since sign-off ({'+' if amount > 0 else '−'}{abs(amount):,.2f})"


def state(point, actual):
    """("open" | "signed" | "changed", status text) for one reference's review point and its actual now."""
    if point is None or point.status != FindingStatus.SIGNED_OFF:
        return "open", "Open"
    moved = actual - (point.signed_amount or ZERO)
    if moved:
        return "changed", _moved(moved)
    who = getattr(point.signed_by, "username", "") or "?"
    when = _local(point.organisation, point.signed_at) if point.signed_at else None
    return "signed", f"✓ Signed off · {who}" + (f" {when.day} {when:%b}" if when else "")


def group_state(group, lines, points, current):
    """A group's state: changed if any account in it changed since its sign-off; else its own sign-off if it has
    one, or signed off once every account in it is."""
    accounts = [line for line in lines if line.group == group and line.code]
    states = [(line, state(points.get(line.code), line.values["cur"])[0]) for line in accounts]
    changed = [line for line, kind in states if kind == "changed"]
    if changed:
        return "changed", _moved(sum((line.values["cur"] - points[line.code].signed_amount for line in changed),
                                     ZERO))
    own = points.get(group)
    if own is not None and own.status == FindingStatus.SIGNED_OFF:
        return state(own, current[group])
    if states and all(kind == "signed" for _, kind in states):
        return "signed", "✓ Signed off"
    return "open", "Open"


def summary(org, period_end):
    """Counts for /status and the review's headline, from the latest figures any /review read for the month, or
    None if nobody has opened it."""
    session = (ReviewSession.objects.filter(organisation=org, period_end=period_end, data__has_key="lines")
               .order_by("-updated_at").first())
    if session is None:
        return None
    lines = _lines(session.data)
    points = _points(org, period_end)
    current = engine.totals(lines, "cur")
    groups = [g for g in engine.GROUPS if any(line.group == g for line in lines)]
    states = {g: group_state(g, lines, points, current)[0] for g in groups}
    changed = [engine.GROUPS[g][0] for g, s in states.items() if s == "changed"]
    requests = {r["code"]: state(points.get(r["code"]), Decimal(r["total"]))[0]
                for r in session.data.get("requests", [])}
    changed += [code for code, s in requests.items() if s == "changed"]
    return {"signed": sum(s == "signed" for s in states.values()), "groups": len(groups), "changed": changed,
            "requests_signed": sum(s == "signed" for s in requests.values()), "requests": len(requests),
            "notes": sum(len(p.notes) for p in points.values()), "as_at": session.data.get("built_at", "")}


# --- Display ----------------------------------------------------------------------------------------------------

def _columns(view):
    return ("ytd", "ytd_" + view["vs"]) if view["ytd"] else ("cur", view["vs"])


def _compare_label(session, view):
    month = session.period_end
    if view["vs"] == "bud":
        return "Budget YTD" if view["ytd"] else "Budget"
    if view["vs"] == "ly":
        return f"Last year YTD ({month.year - 1})" if view["ytd"] else f"Last year ({month:%b} {month.year - 1})"
    last = month.replace(day=1) - timedelta(days=1)
    return f"Last month ({last:%b %Y})"


def _variance(actual, compare):
    diff = actual - compare
    return diff, (diff / abs(compare)) if compare else None


def _as_at(org, data):
    built = data.get("built_at")
    if not built:
        return ""
    when = _local(org, datetime.fromisoformat(built))
    return f" · figures as at {when.day} {when:%b %H:%M}"


def show(ctx, session, *, intro=""):
    data, view = session.data, session.data["view"]
    lines = _lines(data)
    points = _points(ctx.organisation, session.period_end)
    result = Result(f"Review: {session.period_end:%B %Y}",
                    subtitle=f"{session.organisation.name} · {'YTD' if view['ytd'] else 'MTD'} · vs "
                             f"{_compare_label(session, view)}{_as_at(session.organisation, data)}"
                             " · review open: type here")
    if intro:
        result.add(Note(intro))
    counts = summary(ctx.organisation, session.period_end)
    result.add(Figures([
        Figure("Groups signed off", f"{counts['signed']} of {counts['groups']}", TEXT),
        Figure("Changed since sign-off", str(len(counts["changed"])), TEXT, detail=", ".join(counts["changed"])),
        Figure("Review requests signed off", f"{counts['requests_signed']} of {counts['requests']}", TEXT),
        Figure("Notes", str(counts["notes"]), TEXT),
    ]))
    if view["level"] == 0:
        result.add(_l0(session, view, lines, points))
        for r in data.get("requests", []):
            result.add(_request_table(r, points))
    else:
        for group in view["focus"] or [g for g in engine.GROUPS if any(line.group == g for line in lines)]:
            result.add(_l1(session, view, lines, points, group))
    for text in data.get("notes", []):
        result.add(Note(text, "warning"))
    if view["level"] == 0 and data.get("groups"):
        result.add(Note("Groups: " + "; ".join(data["groups"]) + ". Change them in Admin > Review > Review "
                                                                 "settings."))
    result.add(Note(HELP, "warning"))
    return result


def _head(session, view, first):
    return [Column("Ref"), Column(first), Column("Actual", MONEY), Column(_compare_label(session, view), MONEY),
            Column("Var $", MONEY), Column("Var %", PCT), Column("Notes"), Column("Status")]


def _l0(session, view, lines, points):
    actual, compare = _columns(view)
    now, then, current = (engine.totals(lines, c) for c in (actual, compare, "cur"))
    present = {line.group for line in lines}
    table = Table(_head(session, view, "Line"), title="P&L by group")

    def group_rows(*groups):
        for g in groups:
            if g not in present:
                continue
            kind, text = group_state(g, lines, points, current)
            count = sum(len(points[r].notes) for r in [g] + _members(g, lines) if r in points)
            table.add(g, engine.GROUPS[g][0], now[g], then[g], *_variance(now[g], then[g]), count or None, text,
                      tone=TONE[kind])

    def total_row(key, label):
        table.add("", label, now[key], then[key], *_variance(now[key], then[key]), None, "", total=True)

    group_rows("rev", "cogs")
    total_row("gp", "Gross profit")
    group_rows("oth", "lab", "opx")
    total_row("ebitda", "EBITDA")
    group_rows("da", "int", "tax")
    total_row("np", "Net profit")
    for label, key in engine.RATIOS.items():
        a, b = engine.ratio(now, key), engine.ratio(then, key)
        moved = Points(((a - b) * 100).quantize(Decimal("0.1"))) if a is not None and b is not None else None
        table.add("", label, Ratio(a) if a is not None else None, Ratio(b) if b is not None else None, None, moved,
                  None, "")
    return table


def _l1(session, view, lines, points, group):
    actual, compare = _columns(view)
    table = Table(_head(session, view, "Account"), title=f"{engine.GROUPS[group][0]} ({group}) · GL accounts")
    for line in (line for line in lines if line.group == group):
        kind, text = state(points.get(line.code), line.values["cur"]) if line.code else ("open", "")
        note = " · ".join(points[line.code].notes) if line.code in points else ""
        table.add(line.code, line.name, line.values[actual], line.values[compare],
                  *_variance(line.values[actual], line.values[compare]), note or None, text, tone=TONE[kind])
    now, then = engine.totals(lines, actual)[group], engine.totals(lines, compare)[group]
    kind, text = group_state(group, lines, points, engine.totals(lines, "cur"))
    note = " · ".join(points[group].notes) if group in points else ""
    table.add(group, f"Total {engine.GROUPS[group][0].lower()}", now, then, *_variance(now, then), note or None, text,
              total=True, tone=TONE[kind])
    return table


def _request_table(r, points):
    """A review request's result in the layout of its trial run, titled with its status and notes."""
    kind, text = state(points.get(r["code"]), Decimal(r["total"]))
    note = " · ".join(points[r["code"]].notes) if r["code"] in points else ""
    title = f"{r['code']} {r['title']} · {text}" + (f" · Notes: {note}" if note else "")
    if not r.get("rows"):
        table = Table([Column(r["code"])], title=title)
        table.add("Nothing to raise this month.", tone=TONE[kind])
        return table
    columns = [Column(label, kind_) for label, kind_ in r["columns"]]
    table = Table(columns, title=title)
    for row in r["rows"]:
        table.add(*[_cell(v, c.kind) for v, c in zip(row, columns)], tone=TONE[kind])
    return table


def _cell(value, kind):
    if value in (None, ""):
        return None
    if kind in (MONEY, PCT):
        return Decimal(str(value))
    return value


def notes_view(ctx, session, *, every_month=False):
    query = ReviewFinding.objects.filter(organisation=ctx.organisation).exclude(note="")
    if not every_month:
        query = query.filter(period_end=session.period_end)
    result = Result("Review notes" + ("" if every_month else f": {session.period_end:%B %Y}"),
                    subtitle=f"{session.organisation.name} · type notes all for every month, L0 to go back")
    table = result.add(Table([Column("Month"), Column("Ref"), Column("Line"), Column("Note"), Column("Status")]))
    for point in query.order_by("-period_end", "key"):
        for note in point.notes:
            table.add(f"{point.period_end:%b %Y}", point.key, point.label, note, point.get_status_display())
    if not table.rows:
        result.add(Note("No notes yet. Add one with note 4061 <text>."))
    result.add(Note(HELP, "warning"))
    return result
