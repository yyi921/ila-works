"""/accrue: the month-end accrual review.

``/accrue [--Sep 2026]``         the month's proposed accruals, by source, each with its basis and evidence; the
                                 review stays open, so plain input decides: accept 1-5, adjust 3 1,200 <reason>,
                                 reject 4 <reason>, undo 2, why 3, read, refresh, compare, build, done
``/accrue --read [--Sep 2026]``  read the PDFs of the bills that came after month end (in the background)
``/accrue --refresh``            work the proposals out again (decided lines are kept)

The proposals are the month's journal in the Expectation Ledger (Admin > Accounting), worked out from the accrual
profiles: a bill after month end first (placed by its PDF when read), else the estimate from earlier months. A
person accepts, adjusts or rejects each line with a reason; only accepted and adjusted lines go into the journal
file, which is stamped and imported into Xero by a person. Nothing changes in Xero.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from accounting import jobs
from accounting.balance import compare, xero_accruals
from accounting.ledger import LedgerError, build_import_file, run_accruals
from accounting.models import TO_POST, Accrual, Decision, LedgerJournal, State
from accounting.reading import bills_to_read

from .models import AccrueSession
from .output import MONEY, NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from .periods import fmt
from .registry import CommandError, read_params

ZERO = Decimal("0")
CLOSE_WORDS = {"cancel", "/cancel", "stop", "quit", "done", "close", "exit"}
SOURCES = [("evidenced", "Bills after month end"), ("predicted", "Estimates (no bill yet)"),
           ("manual", "Added by a person")]
HELP = ("Type: accept 1-5 (or accept all, accept bills) · adjust 3 1,200 <reason> · reject 4 <reason> · undo 2 · "
        "why 3 · read (bill PDFs) · refresh · compare · build (journal file) · done.")


# --- Dialogue plumbing (see dialogues.py) ------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return (AccrueSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=AccrueSession.Status.OPEN)
            .select_related("journal__accrual").first())


def _close(ctx):
    AccrueSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=AccrueSession.Status.OPEN).update(
        status=AccrueSession.Status.CLOSED)


def cancel(ctx):
    session = open_draft(ctx)
    if session is None:
        return None
    _close(ctx)
    return Result(f"Accrual review closed: {session.journal}",
                  subtitle=f"Decisions are kept. Open it again with /accrue --{session.journal.period_end:%b %Y}.")


def answer(ctx, session, text, files=()):
    journal = session.journal
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if low in CLOSE_WORDS:
        return cancel(ctx)
    word, _, rest = low.partition(" ")
    raw_rest = (text or "").strip()[len(word):].strip()
    lines = numbered(journal)
    if word in ("accept", "approve"):
        chosen = _choose(rest, lines)
        _check_open(journal, chosen)
        for line in chosen:
            if line.decision != Decision.ACCEPTED:
                line.decide(Decision.ACCEPTED, ctx.user)
                line.save()
        intro = f"Accepted {len(chosen)} line(s)."
    elif word in ("adjust", "change"):
        number, amount_text, reason = _split(raw_rest, 3, "adjust 3 1,200 <reason>")
        line = _line(lines, number)
        _check_open(journal, [line])
        line.decide(Decision.ADJUSTED, ctx.user, reason, amount=_money(amount_text))
        line.save()
        intro = f"Line {number} adjusted to {line.expected_amount:,.2f}."
    elif word == "reject":
        number, reason = _split(raw_rest, 2, "reject 4 <reason>")
        line = _line(lines, number)
        _check_open(journal, [line])
        line.decide(Decision.REJECTED, ctx.user, reason)
        line.save()
        intro = f"Line {number} rejected."
    elif word == "undo":
        chosen = _choose(rest, lines)
        _check_open(journal, chosen)
        for line in chosen:
            line.undecide(ctx.user)
            line.save()
        intro = f"{len(chosen)} line(s) back to proposals."
    elif word == "why":
        return why(journal, _line(lines, rest.strip()), rest.strip())
    elif low == "read":
        return start_reading(ctx, journal.accrual, journal.period_end)
    elif low == "refresh":
        journal = _run(ctx, journal.accrual, journal.period_end)
        intro = "Worked out again from the profiles and bills; decided lines were kept."
    elif low == "compare":
        return comparison(journal)
    elif low == "build":
        return build(ctx, journal)
    elif low:
        intro = f'I didn\'t understand "{text.strip()}". {HELP}'
    else:
        intro = ""
    journal.refresh_state()
    return show(journal, intro=intro)


# --- The command --------------------------------------------------------------------------------------------------

def start(ctx, cmd):
    org = ctx.organisation
    if org is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(cmd, ctx, flags={"read", "refresh"}, period=True, exclusive=[("read", "refresh")])
    if cmd.text:
        raise CommandError("Put every option after --, e.g. /accrue --Sep 2026.")
    accrual = Accrual.objects.filter(organisation=org).first()
    if accrual is None or not accrual.profiles.exists():
        raise CommandError(f"{org.name} has no accrual set-up yet. In Admin > Accounting > Accruals, add one (the "
                           "accrual account, e.g. 2000), then run Copy history from Xero, then scan.")
    period_end = _period_end(ctx, params.period)
    if "read" in params.flags:
        return start_reading(ctx, accrual, period_end)
    journal = LedgerJournal.objects.filter(accrual=accrual, kind=LedgerJournal.Kind.ACCRUAL,
                                           period_end=period_end).first()
    intro = ""
    if journal is None or "refresh" in params.flags:
        journal = _run(ctx, accrual, period_end)
        intro = "Proposals worked out from the profiles and the bills after month end."
    _close(ctx)
    AccrueSession.objects.create(organisation=org, user=ctx.user, journal=journal)
    return show(journal, intro=intro)


def _period_end(ctx, period):
    if period is None:
        return ctx.today.replace(day=1) - timedelta(days=1)
    if period.end > ctx.today:
        raise CommandError(f"{period.label} hasn't ended yet.")
    return period.end


def _run(ctx, accrual, period_end):
    try:
        return run_accruals(accrual, period_end, ctx.user)
    except LedgerError as exc:
        raise CommandError(str(exc)) from None


# --- Lines ----------------------------------------------------------------------------------------------------

def _source(line):
    if line.is_manual and line.profile_id is None:
        return "manual"
    return "evidenced" if line.state == State.EVIDENCED or any(e.get("type") == "bill" for e in line.evidence) \
        else "predicted"


def numbered(journal):
    """The journal's lines in the order shown (by source, then account): line n is numbered[n - 1]."""
    order = {key: i for i, (key, _) in enumerate(SOURCES)}
    lines = list(journal.lines.select_related("account", "supplier", "profile", "decided_by"))
    return sorted(lines, key=lambda line: (order[_source(line)], line.account.code, line.pk))


def _choose(text, lines):
    text = text.strip()
    if text in ("all", "everything"):
        return [line for line in lines if not line.decision]
    if text in ("bills", "evidenced"):
        return [line for line in lines if _source(line) == "evidenced" and not line.decision]
    if text in ("estimates", "predicted"):
        return [line for line in lines if _source(line) == "predicted" and not line.decision]
    numbers = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise CommandError(f'"{part}" isn\'t a line number. {HELP}')
        low, high = int(m.group(1)), int(m.group(2) or m.group(1))
        numbers.update(range(min(low, high), max(low, high) + 1))
    if not numbers:
        raise CommandError("Say which lines, e.g. accept 1-5, accept all or accept bills.")
    return [_line(lines, str(n)) for n in sorted(numbers)]


def _line(lines, number):
    if not number.isdigit() or not 1 <= int(number) <= len(lines):
        raise CommandError(f"Choose a line number from 1 to {len(lines)}." if lines else "There are no lines.")
    return lines[int(number) - 1]


def _split(text, parts, example):
    pieces = text.split(None, parts - 1)
    if len(pieces) < parts or not pieces[-1].strip():
        raise CommandError(f"Give the line, then the reason, e.g. {example}.")
    return pieces


def _money(text):
    cleaned = text.replace(",", "").replace("$", "").strip()
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except ArithmeticError:
        raise CommandError(f'"{text}" isn\'t an amount, e.g. 1,200 or 1200.50.') from None


def _check_open(journal, lines):
    if any(line.state in (State.POSTED, State.CLOSED) for line in lines):
        raise CommandError("That line is already posted to Xero; change it in Admin > Expectation Ledger.")


# --- Display --------------------------------------------------------------------------------------------------

def _decision(line):
    if not line.decision:
        return "Proposed"
    text = line.get_decision_display()
    if line.decision == Decision.ADJUSTED and line.proposed_amount is not None:
        text += f" (was {line.proposed_amount:,.2f})"
    return text + (f": {line.decision_reason}" if line.decision_reason else "")


def show(journal, *, intro=""):
    lines = numbered(journal)
    result = Result(f"Accruals at {fmt(journal.period_end)}",
                    subtitle=f"{journal.organisation.name} · {journal.get_state_display()} · review open: type here")
    if intro:
        result.add(Note(intro))
    live = [line for line in lines if line.decision != Decision.REJECTED]
    accepted = [line for line in lines if line.decision in TO_POST]
    review = [s for s in journal.skipped if s.get("review")]
    in_xero = sum(xero_accruals(journal.accrual, journal.period_end).values(), ZERO)
    result.add(Figures([
        Figure("Proposed", sum((line.expected_amount for line in live), ZERO), detail=f"{len(live)} line(s)"),
        Figure("Accepted", sum((line.expected_amount for line in accepted), ZERO),
               detail=f"{len(accepted)} of {len(lines)}; {sum(1 for line in lines if not line.decision)} to decide"),
        Figure("To review", str(len(review)), TEXT, detail="bills the rules couldn't place"),
        Figure("Accrued in Xero", in_xero, detail="for comparison only"),
    ]))
    n = 0
    for key, title in SOURCES:
        group = [line for line in lines if _source(line) == key]
        if not group:
            continue
        table = result.add(Table([Column("#", NUMBER), Column("Account"), Column("Supplier"), Column("Amount", MONEY),
                                  Column("Basis"), Column("Decision")], title=title))
        for line in group:
            n += 1
            table.add(n, f"{line.account.code} {line.account.name}", line.supplier.name if line.supplier_id else "",
                      line.expected_amount, line.basis, _decision(line))
        table.add(None, "Total", "", sum((line.expected_amount for line in group), ZERO), "", "", total=True)
    if review:
        table = result.add(Table([Column("Cost"), Column("Why it needs you")], title="To review"))
        for s in review:
            table.add(s["profile"], s["why"].removeprefix("to review: "))
    _pdf_note(result, journal)
    left = [s for s in journal.skipped if not s.get("review")]
    if left:
        reasons = {}
        for s in left:
            key = s["why"].split(":")[0] if s["why"].startswith(("bill ", "$")) else s["why"]
            reasons.setdefault(key[:70], 0)
            reasons[key[:70]] += 1
        result.add(Note("Left out: " + "; ".join(f"{why} ({count})" for why, count in
                                                 sorted(reasons.items(), key=lambda kv: -kv[1])[:6])
                        + ". Admin > Expectation Ledger lists them all."))
    result.add(Note(HELP, "warning"))
    return result


def _pdf_note(result, journal):
    accrual = journal.accrual
    until = min(timezone.localdate(), journal.period_end + timedelta(days=accrual.bill_window_days))
    unread = len(bills_to_read(accrual, journal.period_end, until))
    if accrual.job_status == jobs.RUNNING:
        result.add(Note(f"Reading bill PDFs: {accrual.job_message} Type refresh when it's done."))
    elif unread:
        result.add(Note(f"{unread} bill(s) after month end have PDFs not read yet. Type read to read them (a few "
                        "minutes, in the background), then refresh.", "warning"))


def why(journal, line, number):
    result = Result(f"Line {number}: {line.account} {line.expected_amount:,.2f}",
                    subtitle=f"{line.get_state_display()} · {_decision(line)}")
    result.add(Note(f"Basis: {line.basis or 'added by a person'}."))
    bills = [e for e in line.evidence if e.get("type") == "bill"]
    if bills:
        table = result.add(Table([Column("Bill"), Column("Supplier"), Column("Dated"), Column("Amount", MONEY),
                                  Column("Placed by"), Column("What it rests on")], title="Evidence"))
        for e in bills:
            rests = e.get("reason", "")
            if e.get("quote"):
                rests += f' · "{e["quote"]}"' + (f" (page {e['page']})" if e.get("page") else "")
            table.add(e.get("number", ""), e.get("supplier", ""), e.get("date", ""), Decimal(e.get("amount") or 0),
                      e.get("placed_by", ""), rests or "billing lag after month end")
    if line.profile_id:
        result.add(Note(f"What iLA learned: {line.profile.memo}"))
        result.add(Note(f"Rule: {line.profile.rule_text}"))
    for entry in line.history[-4:]:
        result.add(Note(f"{entry.get('at', '')[:16].replace('T', ' ')} {entry.get('by', '')}: {entry.get('why', '')}"))
    return result


def comparison(journal):
    rows = compare(journal)
    result = Result(f"Expected vs accrued in Xero, {fmt(journal.period_end)}",
                    subtitle="Xero's accruals are only compared, never used to work out the expectation")
    table = result.add(Table([Column("Account"), Column("Expected", MONEY), Column("Accrued in Xero", MONEY),
                              Column("Difference", MONEY)]))
    for account, expected, actual, diff in rows:
        table.add(f"{account.code} {account.name}", expected, actual, diff)
    table.add("Total", sum((r[1] for r in rows), ZERO), sum((r[2] for r in rows), ZERO),
              sum((r[3] for r in rows), ZERO), total=True)
    return result


def build(ctx, journal):
    try:
        import_file = build_import_file(journal, ctx.user)
    except LedgerError as exc:
        raise CommandError(str(exc)) from None
    result = Result(f"Journal file built: {import_file.file_name}",
                    subtitle=f"{import_file.row_count} rows, stamped. Import it into Xero (Manual journals > Import).")
    result.link, result.link_text = reverse("console:file_download", args=[import_file.pk]), "Download the file"
    result.add(Note("Only accepted and adjusted lines are in it, each with its reversal on day 1 where set. When it's "
                    "imported, mark the journal posted in Admin > Expectation Ledger."))
    return result


def start_reading(ctx, accrual, period_end):
    until = min(ctx.today, period_end + timedelta(days=accrual.bill_window_days))
    todo = len(bills_to_read(accrual, period_end, until))
    if not todo:
        return Result("No bill PDFs to read", subtitle=f"Every bill after {fmt(period_end)} with a PDF on an accrued "
                                                       "account has been read.")
    if not jobs.read_bills(accrual, period_end, user=ctx.user):
        raise CommandError("A job is already running for these accruals; try again when it's done.")
    return Result(f"Reading {todo} bill PDF(s) in the background",
                  subtitle="A few minutes. Then /accrue --refresh (or refresh in an open review) to use them.")
