"""/OJ: compose overlay journals for a month and see the accounts with them on top, in one place.

``/OJ --Oct 2026`` opens the month (``/OJ`` on its own: the one you had open, else last month). It stays open, so
plain input is read as journal commands, and two panels stay on screen: the journal (or the month's bucket) on the
left and the P&L with the journals on top on the right.

    Play    new <narration> · dr 6345 1200 fb DYN wages · cr 2350 · 6345 1200 to 2350 · move 500 from 6345 to 6346
            rev [date|off] · copy 12 · reverse 12
    Tweak   3 1500 · 3 acc 6346 · 3 event TOW · 3 dept ov · 3 desc … · 3 tax … · 3 dr|cr · del 3 · date 31/10
            narr … · undo · redo · versions · restore 2 · open 12 · drop
    Listen  pl · pl lab · bs · why 6345 · list · mute 12 · solo 12 · unmute · refresh
    Finish  file [12|all] (generates the Xero import file: Draft → To be posted) · back 12 · posted 12 · done

Anything else is read as plain English ("accrue audit fee for another 1200", "change the GL 6345 to 6400"): the
language model turns it into the commands above and iLA runs them (see oj_plain). Journals that were right (their
file was generated) are remembered, so the same accounts are used next time.

One bucket, two statuses (see core.overlay). A person's drafts are private; iLA's drafts are seen by everyone and
changing one makes your own copy. Nothing goes to Xero.
"""

import copy as copy_module
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core import overlay
from core.models import Account, AdjustmentLine, Dimension, TrackingOption
from core.overlay import OverlayError
from review import checks as engine
from review.models import ReviewSettings
from xero.api import XeroAPIError
from xero.sync import KINDS, copy_period

from . import llm, oj_plain
from .models import OJSession
from .output import DATE, MONEY, Column, Note, Result, Table
from .periods import add_months, month_period, parse_date
from .pl_by_dimension import period_lines
from .registry import CommandError, read_params

ZERO = Decimal("0")
CLOSE_WORDS = {"done", "close", "exit", "quit", "/cancel"}
HELP = ("Or say it in plain English, e.g. accrue audit fee for another 1200.  Play: new <narration> · dr 6345 1200 fb DYN wages · cr 2350 · 6345 1200 to 2350 · move 500 from 6345 to "
        "6346 · rev · copy 12 · reverse 12.  Tweak: 3 1500 · 3 acc 6346 · 3 event TOW · 3 dept ov · 3 desc … · "
        "del 3 · date 31/10 · narr … · undo · redo · versions · restore 2 · open 12 · drop.  Listen: pl · pl lab · "
        "bs · why 6345 · list · mute 12 · solo 12 · unmute · refresh.  Finish: file · file all · back 12 · done.")
INCOME_CLASSES = (Account.AccountClass.REVENUE,)
BS_CREDIT_CLASSES = (Account.AccountClass.LIABILITY, Account.AccountClass.EQUITY)
P_AND_L = (Account.AccountClass.REVENUE, Account.AccountClass.EXPENSE)


class NotUnderstood(CommandError):
    """Not one of the short commands: it may be plain English."""


AMOUNT_RE = re.compile(r"^\$?(\d[\d,]*(\.\d+)?|\.\d+)(k)?$", re.I)
NEAR_DAYS = 75


# --- Dialogue plumbing (see dialogues.py) ------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None or ctx.user is None:
        return None
    return OJSession.objects.filter(organisation=ctx.organisation, user=ctx.user,
                                    status=OJSession.Status.OPEN).first()


def cancel(ctx):
    session = open_draft(ctx)
    if session is None:
        return None
    OJSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=OJSession.Status.OPEN).update(
        status=OJSession.Status.CLOSED)
    result = Result(f"Overlay journals closed: {session.period_end:%B %Y}",
                    subtitle=f"Everything is saved. Open it again with /OJ --{session.period_end:%b %Y}.")
    result.panel = {"closed": True}
    return result


def start(ctx, cmd):
    if ctx.organisation is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(cmd, ctx, flags={"refresh"}, period=True)
    if cmd.text:
        raise CommandError("Put the month after --, e.g. /OJ --Oct 2026. Then type journal commands without a slash.")
    session = open_draft(ctx)
    if params.period is None:
        period_end = session.period_end if session else ctx.today.replace(day=1) - timedelta(days=1)
    else:
        period = params.period
        if period.start.day != 1 or (period.end + timedelta(days=1)).day != 1 or period.start.month != period.end.month:
            raise CommandError("/OJ works on one whole month, e.g. /OJ --Oct 2026.")
        period_end = period.end
    if session is not None and session.period_end != period_end:
        session.status = OJSession.Status.CLOSED
        session.save()
        session = None
    notes = []
    if session is None:
        session = OJSession(organisation=ctx.organisation, user=ctx.user, period_end=period_end, data={})
    if "xero" not in session.data or "refresh" in params.flags:
        notes = _read_xero(ctx, session)
    session.save()
    return _show(ctx, session, " ".join(notes) or "Type new <narration> to start a journal, or open <number>. "
                                                  "help lists the commands.")


def answer(ctx, session, text, files=()):
    raw = re.sub(r"\s+", " ", (text or "").strip())
    low = raw.lower()
    if low in CLOSE_WORDS:
        return cancel(ctx)
    if "xero" not in session.data:
        _read_xero(ctx, session)
    try:
        try:
            message, extra = _command(ctx, session, raw, low)
        except NotUnderstood:
            message, extra = _plain(ctx, session, raw)
    except OverlayError as exc:
        raise CommandError(str(exc)) from None
    session.save()  # also marks this as the conversation plain input goes to
    return _show(ctx, session, message, **extra)


# --- Reading Xero ---------------------------------------------------------------------------------------------------

def _month(session):
    return month_period(session.period_end.year, session.period_end.month)


def _read_xero(ctx, session):
    """Read the month from Xero and keep it on the session: the P&L by account with its review group, and the
    balance sheet at month end. Also copies the month's transactions (for why) and lets posted journals leave."""
    org, src, month = ctx.organisation, ctx.source, _month(session)
    notes = []
    try:
        copy_period(org, src, KINDS, month.start, min(ctx.today, month.end + timedelta(days=45)))
    except XeroAPIError as exc:
        notes.append(f"Xero's transactions couldn't be copied ({exc}).")
    left, warnings = overlay.settle(org, ctx.user)
    if left:
        notes.append("Now in Xero, so out of the bucket: " + ", ".join(left) + ".")
    notes += warnings
    _refresh_ila_drafts(org, month)
    pl = src.profit_and_loss(month.start, month.end)
    accounts = src.accounts()
    group_of = engine.classify(ReviewSettings.for_organisation(org), accounts)
    rows, groups = {}, {}
    for account, info in accounts.items():
        if info.get("code") and group_of.get(account):
            groups[info["code"]] = group_of[account]
    for account, amount in pl.by_account.items():
        info = accounts.get(account, {})
        code = info.get("code") or f"~{account[:8]}"
        group = group_of.get(account) or engine._fallback(pl.category_of.get(account) or info.get("category"))
        rows[code] = {"name": info.get("name") or pl.names.get(account, ""), "group": group, "amount": str(amount)}
    if pl.unattributed:
        rows["~other"] = {"name": "Not on an account row (e.g. currency gains and losses)", "group": "opx",
                          "amount": str(-pl.unattributed)}
    bs = {}
    try:
        for row in src.balance_sheet(month.end):
            info = accounts.get(row["account_id"] or "", {})
            if info.get("code") and not row["is_total"]:
                bs[info["code"]] = {"name": info["name"], "section": row["section"], "amount": str(row["amount"])}
    except XeroAPIError as exc:
        notes.append(f"The balance sheet couldn't be read ({exc}).")
    session.data = session.data | {"xero": {"pl": rows, "groups": groups, "bs": bs},
                                   "read_at": timezone.now().isoformat(timespec="seconds")}
    return notes


def _refresh_ila_drafts(org, month):
    """Bring iLA's own drafts up to date with what was just read (labour's pay-run journals and its estimate): a
    pay run now in Xero leaves the bucket."""
    from labour import journal, review  # noqa: PLC0415
    from labour.models import PayRun  # noqa: PLC0415

    if not PayRun.objects.filter(organisation=org).exists():
        return
    journal.rebuild_touching(org, month.start, month.end)
    review.build_estimate(org, month.start, month.end, review.stub_days(org, month.start, month.end))


# --- Finding things by what was typed --------------------------------------------------------------------------------

def _amount(token):
    m = AMOUNT_RE.match(token or "")
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return (value * 1000 if m.group(3) else value).quantize(Decimal("0.01"))


def _account(org, token):
    accounts = Account.objects.filter(organisation=org, is_active=True).exclude(code="")
    exact = accounts.filter(code__iexact=token).first()
    if exact:
        return exact
    found = list(accounts.filter(name__icontains=token).order_by("code")[:9])
    if len(found) == 1:
        return found[0]
    if not found:
        raise CommandError(f'No account has the code or name "{token}".')
    raise CommandError(f'"{token}" matches several accounts: ' + ", ".join(f"{a.code} {a.name}" for a in found[:8])
                       + (" …" if len(found) > 8 else "") + ". Use the code.")


def _initials(name):
    return "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in ("and", "of", "the")).lower()


class _Tracking:
    """The organisation's Xero tracking: finds an option by code, the start of its name, its initials or (for
    codes of three letters or more) part of its name, preferring events near the month."""

    def __init__(self, org, month):
        self.month = month
        options = list(TrackingOption.objects.filter(organisation=org, is_active=True, category__is_active=True)
                       .select_related("category").order_by("category__name", "name"))
        self.categories = sorted({o.category.name for o in options})[:2]
        self.options = [o for o in options if o.category.name in self.categories]
        self.values = {}
        for dim in Dimension.objects.filter(organisation=org, is_active=True, tracking_category__isnull=False):
            for value in dim.values.filter(is_active=True, tracking_option__isnull=False):
                self.values[value.tracking_option_id] = value

    def slot(self, option):
        return self.categories.index(option.category.name) + 1

    def _near(self, option):
        value = self.values.get(option.pk)
        if value is None or value.start_date is None:
            return False
        return (value.start_date - timedelta(days=NEAR_DAYS) <= self.month.end
                and value.last_date + timedelta(days=NEAR_DAYS) >= self.month.start)

    def find(self, token, category=None):
        """The option for ``token``, None if nothing matches; CommandError if several do."""
        low = token.lower()
        pool = [o for o in self.options if category is None or o.category.name.lower().startswith(category)]
        for test in (lambda o: o.name.lower() == low,
                     lambda o: o.name.lower().startswith(low)
                     or (len(low) >= 2 and _initials(o.name).startswith(low))
                     or (self.values.get(o.pk) is not None and self.values[o.pk].name.lower().startswith(low)),
                     lambda o: len(low) >= 3 and low in o.name.lower()):
            found = [o for o in pool if test(o)]
            if len(found) > 1:
                near = [o for o in found if self._near(o)]
                found = near if near else found
            if len(found) == 1:
                return found[0]
            if found:
                raise CommandError(f'"{token}" matches several: ' + ", ".join(
                    f"{o.name} ({o.category.name})" for o in found[:8]) + (" …" if len(found) > 8 else "")
                    + ". Type more of it.")
        return None

    def apply(self, line, option):
        slot = self.slot(option)
        setattr(line, f"tracking_name{slot}", option.category.name)
        setattr(line, f"tracking_option{slot}", option.name)
        if option.pk in self.values:
            line.value = self.values[option.pk]


def _tax(word, account):
    low = word.lower()
    if low == "bas":
        return overlay.DEFAULT_TAX
    income = account.account_class in INCOME_CLASSES
    if low == "gst":
        return "GST on Income" if income else "GST on Expenses"
    if low == "gstfree":
        return "GST Free Income" if income else "GST Free Expenses"
    return None


# --- The journal being worked on --------------------------------------------------------------------------------------

def _visible(ctx, session):
    month = _month(session)
    return overlay.in_period(overlay.visible(ctx.organisation, ctx.user), month.start, month.end)


def _get(ctx, session, token):
    number = token.lower().removeprefix("oj-").split("-")[0].lstrip("0") if token else ""
    if not number.isdigit():
        raise NotUnderstood(f'"{token}" isn\'t a journal number. list shows them.')
    found = next((a for a in overlay.visible(ctx.organisation, ctx.user) if a.number == int(number)), None)
    if found is None:
        raise CommandError(f"There's no OJ-{int(number):04d} in the bucket (or it's someone else's draft).")
    return found


def _current(ctx, session, *, to_change=False):
    pk = session.data.get("current")
    adj = next((a for a in overlay.visible(ctx.organisation, ctx.user) if a.pk == pk), None) if pk else None
    if adj is None:
        raise CommandError("No journal is open. Type new <narration> to start one, or open <number>.")
    if not to_change:
        return adj
    if not adj.is_draft:
        raise CommandError(f"{adj.reference} is To be posted, so it's locked. Type back {adj.number} to change it "
                           "(its file is cancelled).")
    if adj.by_ila:  # changing one of iLA's makes your own copy
        mine = overlay.copy(adj, ctx.user, keep_origin=True)
        session.data["current"] = mine.pk
        session.data["copied"] = f"{adj.code} is iLA's, so this is your own copy, {mine.code}. "
        return mine
    return adj


def _line(adj, number):
    line = adj.lines.filter(line_number=int(number)).first()
    if line is None:
        raise CommandError(f"{adj.code} has no line {number}.")
    return line


def _add_line(ctx, session, adj, account, amount, tokens):
    month = _month(session)
    tracking = _Tracking(ctx.organisation, month)
    line = AdjustmentLine(adjustment=adj, line_number=adj.lines.count() + 1, account_code=account.code,
                          amount=amount)
    rest = list(tokens)
    if "|" in rest:  # tracking | description, each tracking option named in full (as plain English writes them)
        cut = rest.index("|")
        tags, rest = " ".join(rest[:cut]), rest[cut + 1:]
        while tags:
            words = tags.split(" ")
            for size in range(len(words), 0, -1):
                piece = " ".join(words[:size])
                tax = _tax(piece, account) if size == 1 else None
                option = None if tax else tracking.find(piece)
                if tax or option is not None:
                    break
            else:
                raise CommandError(f'"{tags}" isn\'t a tracking option in Xero.')
            if tax:
                line.tax_rate = tax
            else:
                tracking.apply(line, option)
            tags = " ".join(words[size:])
        line.description = " ".join(rest)[:255]
        line.save()
        return line
    while rest:
        word = rest[0]
        tax = _tax(word, account)
        option = None if tax else tracking.find(word)
        if tax:
            line.tax_rate = tax
        elif option is not None:
            tracking.apply(line, option)
        else:
            break
        rest.pop(0)
    line.description = " ".join(rest)[:255]
    line.save()
    return line


def _signed(side, amount):
    return amount if side == "dr" else -amount


def _describe(line):
    side = "Dr" if line.amount >= 0 else "Cr"
    tags = " · ".join(t for t in (line.tracking_option1, line.tracking_option2) if t)
    return f"{side} {line.account_code} {abs(line.amount):,.2f}" + (f" ({tags})" if tags else "")


# --- Commands -----------------------------------------------------------------------------------------------------------

def _command(ctx, session, raw, low):  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Carry out one line of typing. Returns (message, extras for the result)."""
    org, user, data = ctx.organisation, ctx.user, session.data
    month = _month(session)
    words = raw.split(" ") if raw else []
    head, rest = (words[0].lower(), words[1:]) if words else ("", [])
    data.pop("copied", None)

    def changed(adj, what):
        overlay.renumber(adj)
        overlay.record(adj, user, what)
        data["left"] = "journal"
        return data.pop("copied", "") + what + ".", {}

    if not raw or head in ("help", "?"):
        return HELP, {}
    if head == "refresh":
        notes = _read_xero(ctx, session)
        return " ".join(notes) or "Read from Xero again.", {}

    # --- Play
    if head == "new":
        narration = " ".join(rest).strip()
        if not narration:
            raise CommandError("Give the narration, e.g. new Wages accrual Oct.")
        adj = overlay.new(org, user, month.end, month.end, narration)
        data["current"], data["left"] = adj.pk, "journal"
        return f"{adj.reference} started, dated {adj.date:%d/%m/%Y}. Add lines: dr 6345 1200 … then cr 2350.", {}
    if head in ("dr", "cr"):
        if not rest:
            raise CommandError(f"Give the account, e.g. {head} 6345 1200.")
        adj = _current(ctx, session, to_change=True)
        account = _account(org, rest[0])
        amount = _amount(rest[1]) if len(rest) > 1 else None
        if amount is None:
            out = adj.out_by
            if not out or (out > 0) != (head == "cr"):
                raise CommandError(f"Give the amount, e.g. {head} {account.code} 1200 (there's nothing for a "
                                   f"{'credit' if head == 'cr' else 'debit'} to balance).")
            amount, tokens = abs(out), rest[1:]
        else:
            tokens = rest[2:]
        line = _add_line(ctx, session, adj, account, _signed(head, amount), tokens)
        return changed(adj, f"Line {line.line_number}: {_describe(line)}")
    m = re.match(r"^(\S+) (\S+) to (\S+)(?: (.*))?$", raw, re.I)
    if m and head != "move" and _amount(m.group(2)) is not None:
        adj = _current(ctx, session, to_change=True)
        debit, credit, amount = _account(org, m.group(1)), _account(org, m.group(3)), _amount(m.group(2))
        tokens = (m.group(4) or "").split()
        first = _add_line(ctx, session, adj, debit, amount, tokens)
        _add_line(ctx, session, adj, credit, -amount, tokens)
        return changed(adj, f"Lines {first.line_number}-{first.line_number + 1}: Dr {debit.code}, Cr {credit.code} "
                            f"{amount:,.2f}")
    m = re.match(r"^move (\S+) from (\S+) to (\S+)(?: (.*))?$", raw, re.I)
    if m:
        amount = _amount(m.group(1))
        if amount is None:
            raise CommandError("Give the amount first, e.g. move 500 from 6345 to 6346.")
        adj = _current(ctx, session, to_change=True)
        source, target = _account(org, m.group(2)), _account(org, m.group(3))
        tokens = (m.group(4) or "").split()
        first = _add_line(ctx, session, adj, target, amount, tokens)
        _add_line(ctx, session, adj, source, -amount, tokens)
        return changed(adj, f"Lines {first.line_number}-{first.line_number + 1}: {amount:,.2f} moved from "
                            f"{source.code} to {target.code}")
    if head == "rev":
        adj = _current(ctx, session, to_change=True)
        if rest and rest[0].lower() == "off":
            adj.reverse_date = None
            return changed(adj, "No reversal")
        if rest:
            day = parse_date(" ".join(rest)) or _short_date(" ".join(rest), adj.date)
            if day is None or day <= adj.date:
                raise CommandError("Give a date after the journal date, e.g. rev 15/11 (or rev on its own for day 1 "
                                   "of next month).")
        else:
            day = add_months(adj.date.replace(day=1), 1)
        adj.reverse_date = day
        return changed(adj, f"Reverses on {day:%d/%m/%Y}")
    if head in ("copy", "reverse"):
        source = _get(ctx, session, rest[0]) if rest else _current(ctx, session)
        twin = overlay.copy(source, user, sign=-1 if head == "reverse" else 1,
                            narration=f"Reversal: {source.narration}" if head == "reverse" else None)
        twin.date = twin.period_end = month.end
        twin.save()
        data["current"], data["left"] = twin.pk, "journal"
        return f"{twin.reference}: {'the reverse' if head == 'reverse' else 'a copy'} of {source.code}.", {}

    # --- Tweak
    if head.isdigit() and rest:
        adj = _current(ctx, session, to_change=True)
        line = _line(adj, head)
        key, value = rest[0].lower(), " ".join(rest[1:])
        amount = _amount(rest[0]) if len(rest) == 1 else None
        account = Account.objects.filter(organisation=org, code=line.account_code).first()
        tracking = _Tracking(org, month)
        if amount is not None:
            line.amount = amount if line.amount >= 0 else -amount
        elif key in ("dr", "cr"):
            new_amount = _amount(rest[1]) if len(rest) > 1 else abs(line.amount)
            if new_amount is None:
                raise CommandError(f"e.g. {head} {key} 1500.")
            line.amount = _signed(key, new_amount)
        elif key in ("acc", "account", "gl") and value:
            line.account_code = _account(org, rest[1]).code
        elif key in ("desc", "description"):
            line.description = value[:255]
        elif key == "tax" and value:
            line.tax_rate = (_tax(value, account) if account else None) or value[:50]
        elif key in ("event", "dept", "department", "track") and value:
            if value.lower() in ("none", "off", "-"):
                slot = next((i for i in (1, 2) if getattr(line, f"tracking_name{i}").lower().startswith(key[:3])),
                            None)
                if slot is None:
                    raise CommandError(f"Line {head} has no {key}.")
                setattr(line, f"tracking_name{slot}", "")
                setattr(line, f"tracking_option{slot}", "")
                if key == "event":
                    line.value = None
            else:
                option = tracking.find(value, None if key == "track" else key[:3])
                if option is None:
                    raise CommandError(f'No {key} matches "{value}".')
                tracking.apply(line, option)
        else:
            raise CommandError(f"To change line {head}: {head} 1500 · {head} acc 6346 · {head} event TOW · "
                               f"{head} dept ov · {head} desc … · {head} tax gst · {head} dr|cr.")
        line.save()
        return changed(adj, f"Line {head}: {_describe(line)}")
    if head in ("del", "delete") and rest and rest[0].isdigit():
        adj = _current(ctx, session, to_change=True)
        _line(adj, rest[0]).delete()
        return changed(adj, f"Line {rest[0]} deleted")
    if head == "date" and rest:
        adj = _current(ctx, session, to_change=True)
        day = parse_date(" ".join(rest)) or _short_date(" ".join(rest), adj.date)
        if day is None:
            raise CommandError("Give a date, e.g. date 31/10 or date 31/10/2026.")
        adj.date = day
        adj.period_end = month_period(day.year, day.month).end
        if adj.reverse_date and adj.reverse_date <= day:
            adj.reverse_date = add_months(day.replace(day=1), 1)
        return changed(adj, f"Dated {day:%d/%m/%Y}")
    if head in ("narr", "narration") and rest:
        adj = _current(ctx, session, to_change=True)
        adj.narration = " ".join(rest)[:255]
        return changed(adj, "Narration changed")
    if head in ("undo", "redo"):
        adj = _current(ctx, session, to_change=True)
        target = adj.version + (-1 if head == "undo" else 1)
        if target < 1 or not adj.versions.filter(number=target).exists():
            raise CommandError(f"Nothing to {head}.")
        was = adj.versions.get(number=adj.version if head == "undo" else target).what
        overlay.go_to(adj, target)
        data["left"] = "journal"
        return f"{'Undone' if head == 'undo' else 'Redone'}: {was}.", {}
    if head == "versions":
        adj = _current(ctx, session)
        data["left"] = "versions"
        return f"{adj.code} has {adj.versions.count()} versions; restore <n> goes back to one.", {}
    if head == "restore" and rest and rest[0].isdigit():
        adj = _current(ctx, session, to_change=True)
        overlay.go_to(adj, int(rest[0]))
        overlay.record(adj, user, f"restored version {rest[0]}")
        data["left"] = "journal"
        return f"{adj.code} is back as it was at version {rest[0]}.", {}
    if head == "open" and rest:
        adj = _get(ctx, session, rest[0])
        data["current"], data["left"] = adj.pk, "journal"
        return f"{adj.reference} open" + (" (iLA's: changing it makes your own copy)." if adj.by_ila else "."), {}
    if head in ("drop", "delete"):
        adj = _get(ctx, session, rest[0]) if rest else _current(ctx, session)
        if adj.by_ila:
            raise CommandError(f"{adj.code} is iLA's; it goes when what it's built from changes. mute {adj.number} "
                               "leaves it out of your view.")
        if adj.made_by_id != user.pk and not user.is_superuser:
            raise CommandError(f"{adj.reference} was made by {adj.made_by}; only they can delete it.")
        reference = adj.reference
        if not adj.is_draft:
            overlay.back_to_draft(adj, user)
        overlay.leave(adj, user, "deleted")
        if data.get("current") == adj.pk:
            data["current"] = None
        data["left"] = "list"
        return f"{reference} deleted.", {}

    # --- Listen
    if head == "list":
        data["left"] = "list"
        return "The month's bucket.", {}
    if head in ("journal", "j"):
        _current(ctx, session)
        data["left"] = "journal"
        return "", {}
    if head == "pl":
        if not rest:
            data["right"] = "pl"
            return "P&L by group.", {}
        group = _group(" ".join(rest))
        data["right"] = f"pl:{group}"
        return f"{engine.GROUPS[group][0]} by account. pl goes back to the groups.", {}
    if head == "bs":
        data["right"] = "bs"
        return "Balance sheet accounts the journals touch, and the rest.", {}
    if head == "why" and rest:
        account = _account(org, rest[0])
        data["right"] = f"why:{account.code}"
        return f"What's behind {account.code} {account.name}. pl goes back.", {}
    if head in ("mute", "solo") and rest:
        adj = _get(ctx, session, rest[0])
        if head == "mute":
            data["muted"] = sorted(set(data.get("muted", [])) | {adj.pk})
            data["solo"] = None
            return f"{adj.code} is left out of the view. unmute brings everything back.", {}
        data["solo"], data["muted"] = adj.pk, []
        return f"Only {adj.code} is in the view. unmute brings everything back.", {}
    if head == "unmute":
        data["muted"], data["solo"] = [], None
        return "Every journal is in the view.", {}

    # --- Finish
    if head == "file":
        if rest and rest[0].lower() == "all":
            chosen = [a for a in _visible(ctx, session) if a.is_draft and a.made_by_id == user.pk]
            if not chosen:
                raise CommandError("You have no drafts of your own this month. file <number> takes one of iLA's.")
        else:
            chosen = [_get(ctx, session, rest[0]) if rest else _current(ctx, session)]
        if not chosen[0].is_draft:
            adj = chosen[0]
            return (f"{adj.reference} already has its file.",
                    {"link": reverse("console:file_download", args=[adj.import_file_id]),
                     "link_text": f"Download {adj.import_file.file_name}"} if adj.import_file_id else {})
        import_file = overlay.generate(org, user, chosen)
        for adj in chosen:  # it was right: remember it for next time
            oj_plain.remember(adj, user, data.get("asked", {}).get(str(adj.pk), ""))
        refs = ", ".join(a.reference for a in chosen)
        data["left"] = "list" if len(chosen) > 1 else "journal"
        return (f"{refs} {'is' if len(chosen) == 1 else 'are'} now To be posted. Download the file and import it in "
                "Xero (Accounting → Manual journals → Import).",
                {"link": reverse("console:file_download", args=[import_file.pk]),
                 "link_text": f"Download {import_file.file_name}"})
    if head == "back" and rest:
        adj = _get(ctx, session, rest[0])
        if adj.is_draft:
            raise CommandError(f"{adj.reference} is already a draft.")
        if adj.made_by_id != user.pk and not user.is_superuser:
            raise CommandError(f"{adj.reference} was made To be posted by {adj.made_by}; only they can take it back.")
        new_file = overlay.back_to_draft(adj, user)
        data["current"], data["left"] = adj.pk, "journal"
        extra = {}
        if new_file is not None:
            extra = {"link": reverse("console:file_download", args=[new_file.pk]),
                     "link_text": f"Download {new_file.file_name}"}
        return (f"{adj.reference} is a draft again and its file is cancelled."
                + (" The other journals in that file have a new one." if new_file else ""), extra)
    if head == "posted" and rest:
        adj = _get(ctx, session, rest[0])
        if adj.is_draft:
            raise CommandError(f"{adj.reference} is a draft; only To be posted journals can be marked as in Xero.")
        reference = adj.reference
        overlay.leave(adj, user, "marked as posted in Xero by hand")
        if data.get("current") == adj.pk:
            data["current"] = None
        data["left"] = "list"
        return f"{reference} has left the bucket. refresh reads Xero again so the figures include it.", {}
    raise NotUnderstood(f'I didn\'t understand "{raw}". {HELP}')


def _plain(ctx, session, raw):
    """Plain English: the language model turns it into the short commands, and iLA runs them as if typed. All of
    them or none: if one fails, nothing changes."""
    if len(raw.split(" ")) < 2 or not llm.available():
        raise CommandError(f'I didn\'t understand "{raw}". ' + ("" if llm.available() else
                           "(Plain English needs the language model: ANTHROPIC_API_KEY isn't set.) ") + HELP)
    pk = session.data.get("current")
    current = next((a for a in overlay.visible(ctx.organisation, ctx.user) if a.pk == pk), None) if pk else None
    steps, question, used = oj_plain.interpret(ctx.organisation, raw, _month(session), current)
    if not steps:
        return (question or "I couldn't turn that into a journal. Say it another way, or type the commands (help "
                            "lists them)."), {"level": "ask"}
    before = copy_module.deepcopy(session.data)
    step = ""
    try:
        with transaction.atomic():
            for step in steps:
                _command(ctx, session, step, step.lower())
    except (CommandError, OverlayError) as exc:
        session.data = before
        raise CommandError(f'I read that as: {" · ".join(steps)}. But "{step}" didn\'t work: {exc} Nothing was '
                           "changed; say it another way, or type the commands.") from None
    now = session.data.get("current")
    if now:
        asked = session.data.setdefault("asked", {})
        asked[str(now)] = (asked.get(str(now), "") + " / " + raw).strip(" /")[:2000]
    session.data["left"] = "journal"
    source = {"memory": "accounts as you used them before", "xero": "accounts as in a similar journal in Xero",
              "chart": "accounts chosen from the chart: check them"}.get(used, "check the accounts")
    return (f"From your words: {' · '.join(steps)}. ({source.capitalize()}; undo steps back.)"
            + (f" {question}" if question else "")), {}


def _short_date(text, near):
    """"31/10" or "15/11": the date nearest the journal's."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", text.strip())
    if not m:
        return None
    try:
        options = [near.replace(year=near.year + shift, month=int(m.group(2)), day=int(m.group(1)))
                   for shift in (0, 1, -1)]
    except ValueError:
        return None
    return min(options, key=lambda d: abs((d - near).days))


def _group(text):
    low = text.lower().strip()
    for key, (label, words) in engine.GROUPS.items():
        if low in (key, label.lower(), *words):
            return key
    raise CommandError(f'"{text}" isn\'t a group. Use ' + ", ".join(engine.GROUPS) + ".")


# --- What the panels show ----------------------------------------------------------------------------------------------

def _in_view(session, journals):
    data = session.data
    if data.get("solo"):
        return [a for a in journals if a.pk == data["solo"]]
    muted = set(data.get("muted", []))
    return [a for a in journals if a.pk not in muted]


class _Figures:
    """The month with the journals on top: per P&L account and group, Xero · To be posted · Draft · After."""

    def __init__(self, ctx, session, journals):
        month = _month(session)
        xero = session.data["xero"]
        self.month = month
        self.effect = overlay.effect(_in_view(session, journals), month.start, month.end)
        codes = set(self.effect) | set(xero["pl"]) | set(xero["bs"])
        self.accounts = {a.code: a for a in Account.objects.filter(organisation=ctx.organisation, code__in=codes)}
        self.xero_pl, self.xero_bs, self.groups = xero["pl"], xero["bs"], xero["groups"]

    def group_of(self, code):
        if code in self.xero_pl:
            return self.xero_pl[code]["group"]
        if code in self.groups:
            return self.groups[code]
        account = self.accounts.get(code)
        if account is None or account.account_class not in P_AND_L:
            return None
        return "rev" if account.account_class in INCOME_CLASSES else "opx"

    def pl_overlay(self, code, status):
        """A journal's effect on a P&L account as the P&L shows it (income and costs both positive)."""
        amount = self.effect[code][status] if code in self.effect else ZERO
        return -amount if self.group_of(code) in engine.INCOME else amount

    def pl_rows(self):
        """{code: (name, group, xero, to post, draft)} for every P&L account with an amount."""
        rows = {}
        for code in set(self.xero_pl) | {c for c in self.effect if self.group_of(c)}:
            info = self.xero_pl.get(code)
            name = info["name"] if info else self.accounts[code].name if code in self.accounts else ""
            rows[code] = (name, self.group_of(code), Decimal(info["amount"]) if info else ZERO,
                          self.pl_overlay(code, "to_post"), self.pl_overlay(code, "draft"))
        return rows

    def totals(self):
        """{group or gp/ebitda/np: [xero, to post, draft]}."""
        out = {g: [ZERO, ZERO, ZERO] for g in engine.GROUPS}
        for _, group, *amounts in self.pl_rows().values():
            for i, amount in enumerate(amounts):
                out[group][i] += amount
        col = lambda f: [f(i) for i in range(3)]  # noqa: E731
        out["gp"] = col(lambda i: out["rev"][i] - out["cogs"][i])
        out["ebitda"] = col(lambda i: out["gp"][i] + out["oth"][i] - out["lab"][i] - out["opx"][i])
        out["np"] = col(lambda i: out["ebitda"][i] - out["da"][i] - out["int"][i] - out["tax"][i])
        return out


PL_COLUMNS = [Column("Xero", MONEY), Column("To be posted", MONEY), Column("Draft", MONEY), Column("After", MONEY)]
PL_LAYOUT = (("rev", 0), ("cogs", 0), ("gp", 1), ("oth", 0), ("lab", 0), ("opx", 0), ("ebitda", 1), ("da", 0),
             ("int", 0), ("tax", 0), ("np", 1))
SUBTOTALS = {"gp": "Gross profit", "ebitda": "EBITDA", "np": "Net profit"}


def _money_row(table, *label, amounts, total=False, touched=False):
    xero, to_post, draft = amounts
    table.add(*label, xero, to_post or None, draft or None, xero + to_post + draft, total=total,
              tone="touch" if touched and not total else "")


def _right(ctx, session, figures, journals):
    view = session.data.get("right", "pl")
    month = figures.month
    if view.startswith("pl:"):
        group = view[3:]
        table = Table([Column("Code"), Column("Account"), *PL_COLUMNS],
                      title=f"{engine.GROUPS[group][0]} · {month.label}")
        sums = [ZERO, ZERO, ZERO]
        for code, (name, g, *amounts) in sorted(figures.pl_rows().items()):
            if g == group:
                _money_row(table, "" if code.startswith("~") else code, name, amounts=amounts,
                           touched=bool(amounts[1] or amounts[2]))
                sums = [a + b for a, b in zip(sums, amounts)]
        _money_row(table, "", f"Total {engine.GROUPS[group][0].lower()}", amounts=sums, total=True)
        return f"P&L · {engine.GROUPS[group][0]}", [table]
    if view == "bs":
        table = Table([Column("Code"), Column("Account"), *PL_COLUMNS], title=f"Balance sheet at {month.end:%d %b %Y}")
        codes = set(figures.xero_bs) | {c for c in figures.effect if figures.group_of(c) is None}
        np_effect = [ZERO, ZERO]
        for code in sorted(codes, key=lambda c: (c not in figures.effect, c)):
            info, account = figures.xero_bs.get(code), figures.accounts.get(code)
            sign = -1 if account is not None and account.account_class in BS_CREDIT_CLASSES else 1
            eff = figures.effect.get(code, {"to_post": ZERO, "draft": ZERO})
            amounts = [Decimal(info["amount"]) if info else ZERO, sign * eff["to_post"], sign * eff["draft"]]
            _money_row(table, code, info["name"] if info else account.name if account else "", amounts=amounts,
                       touched=code in figures.effect)
        totals = figures.totals()["np"]
        np_effect = [ZERO, totals[1], totals[2]]
        if np_effect[1] or np_effect[2]:
            _money_row(table, "", "Net profit for the month, from the journals (equity)", amounts=np_effect,
                       touched=True)
        return "Balance sheet", [table]
    if view.startswith("why:"):
        return _why(ctx, session, figures, journals, view[4:])
    table = Table([Column("P&L"), *PL_COLUMNS], title=f"P&L · {month.label}")
    totals = figures.totals()
    for key, is_total in PL_LAYOUT:
        label = SUBTOTALS.get(key) or engine.GROUPS[key][0]
        if not is_total and not any(totals[key]):
            continue
        _money_row(table, label, amounts=totals[key], total=bool(is_total),
                   touched=bool(totals[key][1] or totals[key][2]))
    return "P&L", [table]


def _why(ctx, session, figures, journals, code):
    month = figures.month
    account = figures.accounts.get(code) or Account.objects.filter(organisation=ctx.organisation, code=code).first()
    table = Table([Column("Date", DATE), Column("From"), Column("Description"), Column("Debit", MONEY),
                   Column("Credit", MONEY)], title=f"{code} {account.name if account else ''} · {month.label}")
    is_income = account is not None and account.account_class in INCOME_CLASSES
    total = ZERO
    for pl in period_lines(ctx.organisation, month.start, month.end, pl_only=False):
        if pl.account.code != code:
            continue
        debit = -pl.amount if is_income else pl.amount
        parent = pl.parent
        source = getattr(parent, "number", "") or getattr(parent, "reference", "") or (
            "journal" if hasattr(parent, "narration") else "bank")
        text = getattr(parent, "narration", "") or pl.line.description
        table.add(getattr(parent, "date", None), f"Xero {source}"[:40], (text or "")[:70],
                  debit if debit >= 0 else None, -debit if debit < 0 else None)
        total += debit
    for adj in _in_view(session, journals):
        for sign, day in ((1, adj.date), (-1, adj.reverse_date)):
            if not day or not month.start <= day <= month.end:
                continue
            for line in adj.lines.all():
                if line.account_code == code:
                    amount = sign * line.amount
                    table.add(day, adj.reference + (" reversal" if sign < 0 else ""),
                              (line.description or adj.narration)[:70], amount if amount >= 0 else None,
                              -amount if amount < 0 else None, tone="touch")
                    total += amount
    table.add(None, "Net", "Xero's transactions as iLA copied them, plus the journals", total if total >= 0 else None,
              -total if total < 0 else None, total=True)
    return f"Why {code}", [table]


def _journal_blocks(adj):
    lines = list(adj.lines.all())
    show_tax = any(line.tax_rate and line.tax_rate != overlay.DEFAULT_TAX for line in lines)
    names = dict(Account.objects.filter(organisation=adj.organisation, code__in={l.account_code for l in lines})
                 .values_list("code", "name"))
    columns = [Column("#"), Column("Account"), Column("Tracking"), Column("Debit", MONEY), Column("Credit", MONEY),
               Column("Description")]
    if show_tax:
        columns.insert(3, Column("Tax"))
    table = Table(columns, title=f"{adj.reference} · {adj.date:%d/%m/%Y} · {adj.narration}")
    debits = credits = ZERO
    for line in lines:
        tracking = " · ".join(t for t in (line.tracking_option1, line.tracking_option2) if t)
        cells = [str(line.line_number), f"{line.account_code} {names.get(line.account_code, '')}"[:38], tracking,
                 line.amount if line.amount > 0 else None, -line.amount if line.amount < 0 else None,
                 line.description[:60]]
        if show_tax:
            cells.insert(3, line.tax_rate or overlay.DEFAULT_TAX)
        table.add(*cells)
        debits += max(line.amount, ZERO)
        credits += max(-line.amount, ZERO)
    total = ["", "Total", "", debits, credits, ""]
    if show_tax:
        total.insert(3, "")
    table.add(*total, total=True)
    blocks = [table]
    if not lines:
        blocks.append(Note("No lines yet. dr 6345 1200 adds a debit; cr 2350 with no amount takes the balance."))
    elif debits != credits:
        out = debits - credits
        blocks.append(Note(f"Out by {abs(out):,.2f}: {'credit' if out > 0 else 'debit'} needed.", "warning"))
    facts = [("iLA's draft (" + (adj.evidence or adj.origin) + "): changing it makes your own copy") if adj.by_ila
             else f"Made by {adj.made_by}"]
    facts.append(f"Reverses on {adj.reverse_date:%d/%m/%Y}" if adj.reverse_date else "Doesn't reverse")
    if not adj.is_draft:
        facts.append(f"To be posted: locked (back {adj.number} to change it)")
    blocks.append(Note(" · ".join(facts) + "."))
    return blocks


def _list_table(ctx, session, figures, journals):
    month = figures.month
    table = Table([Column("Number"), Column("Date", DATE), Column("Narration"), Column("Made by"),
                   Column("Debits", MONEY), Column("Net profit", MONEY), Column("Reverses", DATE), Column("")],
                  title=f"The bucket · {month.label}")
    data = session.data
    hidden = set(data.get("muted", []))
    for adj in journals:
        effect = overlay.effect([adj], month.start, month.end)
        profit = ZERO
        for code, amounts in effect.items():
            group = figures.group_of(code)
            if group:
                profit -= amounts["draft"] + amounts["to_post"]
        out = (data.get("solo") and data["solo"] != adj.pk) or adj.pk in hidden
        state = "muted" if out else "open" if adj.pk == data.get("current") else ""
        actions = [("Open", f"open {adj.number}")]
        if adj.is_draft:
            actions.append(("Generate Xero file", f"file {adj.number}"))
        else:
            actions.append(("Back to draft", f"back {adj.number}"))
        table.add(adj.reference, adj.date, adj.narration[:50], "iLA" if adj.by_ila else str(adj.made_by),
                  adj.debit_total, profit, adj.reverse_date, state, tone="ok" if not adj.is_draft else "",
                  actions=actions)
    return table


def _versions_table(adj):
    table = Table([Column("Version"), Column("Change"), Column("When"), Column("")],
                  title=f"{adj.code}: versions")
    for version in adj.versions.all():
        table.add(str(version.number), version.what, f"{timezone.localtime(version.created_at):%d/%m %H:%M}",
                  "shown" if version.number == adj.version else "",
                  actions=[] if version.number == adj.version else [("Restore", f"restore {version.number}")])
    return table


def _show(ctx, session, message="", *, link="", link_text="", level="info"):
    journals = _visible(ctx, session)
    data = session.data
    current = next((a for a in overlay.visible(ctx.organisation, ctx.user) if a.pk == data.get("current")), None)
    if current is None and data.get("current"):
        data["current"] = None
    figures = _Figures(ctx, session, journals)
    month = figures.month
    totals = figures.totals()["np"]
    left_view = data.get("left") or "list"
    if current is None or left_view == "list":
        left_title, left = "The bucket", [_list_table(ctx, session, figures, journals)]
        if not journals:
            left.append(Note("No overlay journals in this month yet. new <narration> starts one."))
    elif left_view == "versions":
        left_title, left = f"{current.code} versions", [_versions_table(current)]
    else:
        left_title, left = current.reference, _journal_blocks(current)
    right_title, right = _right(ctx, session, figures, journals)
    drafts = sum(1 for a in journals if a.is_draft)
    in_view = _in_view(session, journals)
    result = Result(f"/OJ · {month.label}", subtitle=message)
    result.link, result.link_text = link, link_text
    result.panel = {
        "title": f"OJ · {month.label} · {ctx.organisation.name}",
        "headline": [{"label": "Net profit · Xero", "value": totals[0]},
                     {"label": "To be posted", "value": totals[1]}, {"label": "Draft", "value": totals[2]},
                     {"label": "After", "value": sum(totals, ZERO)}],
        "status": f"{len(journals)} journal{'s' if len(journals) != 1 else ''}: {drafts} draft, "
                  f"{len(journals) - drafts} to be posted"
                  + (f" · {len(journals) - len(in_view)} left out of the view" if len(in_view) != len(journals) else ""),
        "left": {"title": left_title, "blocks": left}, "right": {"title": right_title, "blocks": right},
        "message": message, "level": level,
    }
    session.save()
    return result
