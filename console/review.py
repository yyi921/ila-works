"""/review: the month-end P&L review, iLA + a person.

``/review [--Oct 2026]``  the core checks (C1–C8) and every confirmed review request (RR-01…) for the month (default
                          the last full month), as one numbered list of findings, each with iLA's reading. The review
                          stays open, so plain input works it: why 3 · note 4 <text> · ok 1-5 · later 2 · act 6
                          [/command] · refresh · done

Checks, materiality and the accounts behind the ratios are set in Admin > Review; review requests are added and trial
run there too. A finding is reviewed once it's OK'd, noted or acted on; statuses and notes are kept for the month
and notes come back when the same subject comes up again. Nothing changes in Xero.
"""

import re
from datetime import timedelta
from decimal import Decimal

from accounting.models import TO_POST, Expectation, State
from review import checks as engine
from review.models import CHECKS, REVIEWED, FindingStatus, ReviewFinding, ReviewSettings

from .models import ReviewSession
from .output import MONEY, NUMBER, PCT, TEXT, Column, Figure, Figures, Note, Result, Table
from .periods import month_period
from .registry import CommandError, read_params

ZERO = Decimal("0")
CLOSE_WORDS = {"cancel", "/cancel", "stop", "quit", "done", "close", "exit"}
HELP = ("Type: why 3 · note 4 <what iLA can't know> · ok 1-5 (or ok all) · later 2 · act 6 [/command] · refresh · "
        "done.")
STATUS_TEXT = {"open": "Open", "later": "Later", "ok": "OK", "noted": "Noted", "acted": "Acted on"}


# --- Dialogue plumbing (see dialogues.py) ------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return ReviewSession.objects.filter(organisation=ctx.organisation, user=ctx.user,
                                        status=ReviewSession.Status.OPEN).first()


def _close(ctx):
    ReviewSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=ReviewSession.Status.OPEN).update(
        status=ReviewSession.Status.CLOSED)


def cancel(ctx):
    session = open_draft(ctx)
    if session is None:
        return None
    _close(ctx)
    return Result(f"Review closed: {session.period_end:%B %Y}",
                  subtitle=f"Statuses and notes are kept. Open it again with /review --{session.period_end:%b %Y}.")


def answer(ctx, session, text, files=()):
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if low in CLOSE_WORDS:
        return cancel(ctx)
    word, _, rest = low.partition(" ")
    raw_rest = (text or "").strip()[len(word):].strip()
    findings = [engine.Finding.from_json(f) for f in session.data.get("findings", [])]
    intro = ""
    if word == "why":
        return why(session, _pick(findings, rest.strip()), rest.strip())
    if word in ("ok", "later"):
        chosen = _choose(findings, rest)
        status = FindingStatus.OK if word == "ok" else FindingStatus.LATER
        for f in chosen:
            _decide(ctx, session, f, status)
        intro = f"{len(chosen)} finding(s) marked {STATUS_TEXT[status]}."
    elif word == "note":
        number, _, note = raw_rest.partition(" ")
        if not note.strip():
            raise CommandError("Give the finding and the note, e.g. note 4 PBR needed extra security staff.")
        f = _pick(findings, number)
        _decide(ctx, session, f, FindingStatus.NOTED, note=note.strip())
        intro = f"Note kept on {number}. It comes back whenever {f.label} comes up again."
    elif word == "act":
        number, _, command = raw_rest.partition(" ")
        f = _pick(findings, number)
        command = command.strip()
        _decide(ctx, session, f, FindingStatus.ACTED, action=command or suggest(f, session.period_end))
        if command:
            return _run(ctx, command)
        return _suggestion(f, number, session.period_end)
    elif low == "refresh":
        session = _build(ctx, session.period_end, session)
        intro = "Worked out again from Xero; statuses and notes were kept."
        findings = None
    elif low:
        intro = f'I didn\'t understand "{text.strip()}". {HELP}'
    if findings is not None:
        session.data = session.data | {"findings": [f.to_json() for f in findings]}
        session.save()
    return show(session, intro=intro)


# --- The command --------------------------------------------------------------------------------------------------

def start(ctx, cmd):
    org = ctx.organisation
    if org is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(cmd, ctx, flags={"refresh"}, period=True)
    if cmd.text:
        raise CommandError("Put every option after --, e.g. /review --Oct 2026.")
    period_end = _period_end(ctx, params.period)
    _close(ctx)
    session = _build(ctx, period_end)
    return show(session)


def _period_end(ctx, period):
    if period is None:
        return ctx.today.replace(day=1) - timedelta(days=1)
    if period.start.day != 1 or (period.end + timedelta(days=1)).day != 1 or period.start.month != period.end.month:
        raise CommandError("/review looks at one whole month, e.g. /review --Oct 2026.")
    if period.end >= ctx.today:
        raise CommandError(f"{period.label} hasn't finished yet; /review looks at a full month.")
    return period.end


def _build(ctx, period_end, session=None):
    """Work out the month's findings (core checks, then review requests) and keep them on the session."""
    org, src = ctx.organisation, ctx.source
    settings = ReviewSettings.for_organisation(org)
    month = month_period(period_end.year, period_end.month)
    figures = engine.gather(src, month, ctx.fy)
    groups = engine.account_groups(settings, figures.accounts)
    found = engine.core_findings(org, figures, {c.code: c for c in settings.checks.all()}, groups,
                                 accrued=_accrued(org, period_end))
    notes = []
    if figures.budget is None:
        notes.append("No budget in Xero for this month (or no permission to read it): C3, C4 and the budget "
                     "comparisons were skipped.")
    from review.requests import run_confirmed

    found += run_confirmed(org, src, month, ctx.fy, notes)
    _merge(org, period_end, found)
    data = {"findings": [f.to_json() for f in found], "notes": notes,
            "groups": engine.describe_groups(groups, figures.accounts),
            "checks": {c.code: [c.enabled, c.materiality_text()] for c in settings.checks.all()}}
    if session is None:
        session = ReviewSession.objects.create(organisation=org, user=ctx.user, period_end=period_end, data=data)
    else:
        session.data = data
        session.save()
    return session


def _accrued(org, period_end):
    """{account code: amount accepted or adjusted in /accrue for the month, not yet posted}."""
    out = {}
    for line in (Expectation.objects.filter(organisation=org, journal__period_end=period_end, decision__in=TO_POST)
                 .exclude(state__in=(State.POSTED, State.CLOSED)).select_related("account")):
        out[line.account.code] = out.get(line.account.code, ZERO) + line.expected_amount
    return out


def _merge(org, period_end, found):
    saved = {f.key: f for f in ReviewFinding.objects.filter(organisation=org, period_end=period_end)}
    for f in found:
        if f.key in saved:
            f.status, f.note, f.action = saved[f.key].status, saved[f.key].note, saved[f.key].action


def _decide(ctx, session, f, status, *, note=None, action=None):
    obj, _ = ReviewFinding.objects.get_or_create(organisation=ctx.organisation, period_end=session.period_end,
                                                 key=f.key, defaults={"subject": f.subject, "label": f.label[:255]})
    obj.status = status
    if note is not None:
        obj.note = note
        f.note = note
    if action is not None:
        obj.action = action[:200]
        f.action = obj.action
    obj.updated_by = ctx.user
    obj.save()
    f.status = status


# --- Choosing findings --------------------------------------------------------------------------------------------

def _pick(findings, number):
    if not number.isdigit() or not 1 <= int(number) <= len(findings):
        raise CommandError(f"Choose a finding from 1 to {len(findings)}." if findings else "There are no findings.")
    return findings[int(number) - 1]


def _choose(findings, text):
    text = text.strip()
    if text in ("all", "everything"):
        return [f for f in findings if f.status not in REVIEWED]
    numbers = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise CommandError(f'"{part}" isn\'t a finding number. {HELP}')
        low, high = int(m.group(1)), int(m.group(2) or m.group(1))
        numbers.update(range(min(low, high), max(low, high) + 1))
    if not numbers:
        raise CommandError("Say which findings, e.g. ok 1-5 or ok all.")
    return [_pick(findings, str(n)) for n in sorted(numbers)]


# --- Acting -----------------------------------------------------------------------------------------------------

def suggest(f, period_end):
    """The command most likely to fix a finding."""
    month = f"{period_end:%b %Y}"
    if f.check.startswith("RR-"):
        return (f.action or "").strip() or f"/pl --{month}"
    if f.subject in ("ratio:labour",):
        return f"/accrue --{month}"
    if f.subject == "ratio:cogs" or f.category in ("cost_of_sales", "expenses"):
        return f"/accrue --{month}"
    if f.category in ("revenue", "other_income"):
        return f"/compare --invoice --{month}"
    return f"/pl --{month}"


def _suggestion(f, number, period_end):
    command = suggest(f, period_end)
    result = Result(f"Finding {number} marked acted on", subtitle=f.label)
    result.add(Note(f"Next: {command}. Or type act {number} /<command> to run another one now."))
    return result


def _run(ctx, line):
    from .parser import parse
    from .registry import lookup

    if not line.startswith("/"):
        raise CommandError("Give the command with its slash, e.g. act 3 /accrue --Oct 2026.")
    cmd = parse(line)
    spec = lookup(cmd.name)
    if spec is None or cmd.name == "review":
        raise CommandError(f"There's no /{cmd.name} command to run from the review.")
    return spec.func(ctx, cmd)


# --- Display ----------------------------------------------------------------------------------------------------

def _cap(text):
    return text[:1].upper() + text[1:]


def _status(f):
    text = STATUS_TEXT.get(f.status, f.status)
    if f.note:
        text += f": {f.note}"
    elif f.status == "acted" and f.action:
        text += f" ({f.action})"
    return text


def show(session, *, intro=""):
    findings = [engine.Finding.from_json(f) for f in session.data.get("findings", [])]
    data = session.data
    result = Result(f"Review: {session.period_end:%B %Y}",
                    subtitle=f"{session.organisation.name} · P&L · review open: type here")
    if intro:
        result.add(Note(intro))
    reviewed = sum(1 for f in findings if f.status in REVIEWED)
    later = sum(1 for f in findings if f.status == FindingStatus.LATER)
    result.add(Figures([
        Figure("Findings", str(len(findings)), TEXT, detail="past materiality"),
        Figure("Reviewed", str(reviewed), TEXT, detail="OK'd, noted or acted on"),
        Figure("Open", str(len(findings) - reviewed), TEXT, detail=f"{later} marked later" if later else ""),
    ]))
    n = 0
    quiet = []
    for code, (name, kind, _) in CHECKS.items():
        enabled, materiality = data.get("checks", {}).get(code, [True, ""])
        if not enabled:
            continue
        group = [f for f in findings if f.check == code]
        if not group:
            quiet.append(code)
            continue
        title = f"{code} {name} ({materiality})"
        if kind == "ratio":
            table = result.add(Table([Column("#", NUMBER), Column("Finding"), Column("Now", PCT),
                                      Column("Compared", PCT),
                                      Column("Points", TEXT), Column("Worth", MONEY), Column("iLA's reading"),
                                      Column("Status")], title=title))
            for f in group:
                n += 1
                table.add(n, f.label, f.current, f.compare, f"{f.movement:+.1f}", f.effect, f.reading, _status(f))
        else:
            table = result.add(Table([Column("#", NUMBER), Column("Line"), Column("Now", MONEY),
                                      Column(_cap(engine.COMPARED[group[0].compared]), MONEY),
                                      Column("Fav/(Unfav)", MONEY), Column("iLA's reading"), Column("Status")],
                                     title=title))
            for f in group:
                n += 1
                table.add(n, f.label, f.current, f.compare, f.movement, f.reading, _status(f), total=f.is_total)
    for f in findings:
        if not f.check.startswith("RR-"):
            continue
        n += 1
        result.add(_request_table(f, n))
    if quiet:
        result.add(Note(f"Nothing past materiality in {', '.join(quiet)}."))
    for text in data.get("notes", []):
        result.add(Note(text, "warning"))
    if data.get("groups"):
        result.add(Note("Ratios use " + "; ".join(data["groups"]) + ". Change them in Admin > Review > Review "
                                                                    "settings."))
    result.add(Note(HELP, "warning"))
    return result


def _request_table(f, n):
    """A review request's result in the layout of its trial run, numbered as one finding."""
    columns = [Column(label, kind) for label, kind in f.table.get("columns", [])]
    title = f"{n} · {f.check} {f.label}: {f.reading} · {_status(f)}"
    table = Table(columns, title=title)
    for row in f.table.get("rows", []):
        table.add(*[_cell(v, c.kind) for v, c in zip(row, columns)])
    return table


def _cell(value, kind):
    if value in (None, ""):
        return None
    if kind in (MONEY, PCT):
        return Decimal(str(value))
    return value


def why(session, f, number):
    result = Result(f"Finding {number}: {f.label}", subtitle=f"{f.check} {CHECKS.get(f.check, [f.check])[0]} · "
                                                             f"{_status(f)}")
    if f.reading:
        result.add(Note(f"iLA's reading: {f.reading}."))
    if f.table:
        result.add(_request_table(f, int(number)))
        return result
    kind = TEXT if f.is_ratio else MONEY
    if f.is_ratio:
        result.add(Figures([Figure("Now", f.current, PCT), Figure(_cap(engine.COMPARED[f.compared]), f.compare, PCT),
                            Figure("Worth", f.effect, detail="the move × this month's revenue")]))
    else:
        result.add(Figures([Figure("Now", f.current), Figure(_cap(engine.COMPARED[f.compared]), f.compare),
                            Figure("Fav/(Unfav)", f.movement)]))
    if f.series:
        table = result.add(Table([Column("Month"), Column("%" if f.is_ratio else "Amount", kind)],
                                 title="The last 13 months"))
        for label, value in f.series:
            table.add(label, (f"{value}%" if value else "") if f.is_ratio else Decimal(value or 0))
    if f.note:
        result.add(Note(f"Your note: {f.note}"))
    return result

