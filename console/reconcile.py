"""/reconcile: month-end balance sheet reconciliation.

``/reconcile [--Sep 2026]``          the pack: every balance sheet account to reconcile at the period end, its Xero
                                     balance, supporting balance, reconciling items, difference and sign-off
``/reconcile 090 [--Sep 2026]``      open one account's working paper (by code or name)
``/reconcile --bank``                bank accounts: Xero transactions not yet reconciled (what /recon used to show)
``/reconcile 1400 --Sep 2026 << schedule.csv``   the schedule supporting one account (prepayments, accruals, loans...)
``/reconcile --Sep 2026 << schedules.csv``       schedules for several accounts at once (an Account column)
``/reconcile --template``            the schedule template and how to fill it in

How each account is reconciled, and from what balance it is material, is preset per account by an admin in the
chart of accounts. The Xero balance should equal the supporting balance (a bank statement's closing balance, the
debtors or creditors ledger, nil, or a figure from a schedule or a third-party statement) plus the reconciling items.
A working paper is signed as prepared once the difference is nil, then reviewed; any change after that, including a
change in the Xero balance, clears the sign-off. Reconciling items roll forward to the next period. Nothing changes
in Xero.

A schedule is the list of lines making up a supporting balance. It is read from the uploaded CSV, checked and kept
on the working paper (with the file's name and SHA-256); the file itself isn't stored. A schedule is for one month:
next month needs the next schedule.

The account accrual runs post to (the first account in the organisation's accrual set-up, e.g. TECC 2000) takes its
schedule from the Expectation Ledger: that month's accrual lines marked posted (or since closed), unless a person
uploads a schedule or types a supporting balance instead.
"""

import csv
import hashlib
import io
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.models import Account, AuditLog, ReconMethod, Reconciliation
from xero.api import XeroAPIError

from .models import ReconSession
from .output import DATE, MONEY, NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from .periods import fmt, parse_date
from .registry import CommandError, read_params

ZERO = Decimal("0")
CENT = Decimal("0.01")
CLOSE_WORDS = {"cancel", "/cancel", "stop", "quit", "done", "close", "exit"}
SUPPORT_LABEL = {ReconMethod.BANK: "Statement balance", ReconMethod.SUBLEDGER: "Ledger balance",
                 ReconMethod.NIL: "Should be"}
AUTO_SUPPORT = {ReconMethod.NIL, ReconMethod.SUBLEDGER}
# Pack statuses, in the order the pack lists them (work to do first).
DIFFERENCE, NO_SUPPORT, NOT_STARTED, NOT_SET, READY, PREPARED, REVIEWED, NIL_OK, BELOW = (
    "Difference", "No support", "Not started", "Method not set", "Ready to sign", "Prepared", "Reviewed", "Nil",
    "Below threshold")
ORDER = [DIFFERENCE, NO_SUPPORT, NOT_STARTED, NOT_SET, READY, PREPARED, REVIEWED, NIL_OK, BELOW]
DONE = {REVIEWED, NIL_OK, BELOW}
BANK_NOTE = ("Xero doesn't say when an item was reconciled, so an item dated before the period end but reconciled "
             "since (e.g. a deposit in transit that cleared on the 2nd) isn't listed: add it with add.")
HELP = ("Type: statement 12,345.67 (bank) or support 5,000 · << schedule.csv · add -250.00 Unpresented cheque 123 · "
        "add 30/09/2026 1,200 Deposit in transit · remove 2 · note <text> · prepared · reviewed · reopen · next · done.")
# Methods whose supporting balance a person gives: typed, or as a schedule.
SCHEDULE_METHODS = {ReconMethod.SCHEDULE, ReconMethod.FIXED_ASSETS, ReconMethod.GST, ReconMethod.INTERCOMPANY}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEMPLATE_ROWS = [
    ["Account", "Description", "Reference", "Amount", "From", "To", "Notes"],
    ["1400", "Public liability insurance", "INV-5521", "6,187.50", "01/07/2026", "30/06/2027", "9 of 12 months left"],
    ["1400", "Ticketing software licence", "TS-2026", "2,400.00", "01/01/2026", "31/12/2026", "3 of 12 months left"],
    ["2100", "Audit fee FY2027", "", "4,500.00", "", "", "Quote from auditors, 3 of 12 months"],
    ["2100", "Electricity September", "", "18,000.00", "01/09/2026", "30/09/2026", "Bill due mid October"],
    ["2300", "Bank loan", "Loan 4471", "50,000.00", "", "", "Per lender statement 30/09/2026"],
]
GUIDE_ROWS = (
    ("Account", "The account code, e.g. 1400. Can be left out when the command names the account."),
    ("Description", "What the line is, e.g. Public liability insurance."),
    ("Reference", "Optional: invoice, contract or loan number."),
    ("Amount", "The line's balance at the period end, signed as the account shows it in Xero's balance sheet."),
    ("From, To", "Optional: the period the line covers (30/06/2027). A line whose To date is on or before the "
                 "period end is flagged: a prepayment that has run out should be nil."),
    ("Notes", "Optional: how it was worked out, e.g. 9 of 12 months left."),
)


# --- Dialogue plumbing (see dialogues.py) ------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return (ReconSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=ReconSession.Status.OPEN)
            .select_related("reconciliation__account").first())


def cancel(ctx):
    session = open_draft(ctx)
    if session is None:
        return None
    _close(ctx)
    rec = session.reconciliation
    return Result(f"Working paper closed: {rec.account}", subtitle="What you entered is kept. Open it again with "
                                                                   f"/reconcile {rec.account.code} --{_label(rec.period_end)}.")


def _close(ctx):
    ReconSession.objects.filter(organisation=ctx.organisation, user=ctx.user, status=ReconSession.Status.OPEN).update(
        status=ReconSession.Status.CLOSED)


def answer(ctx, session, text, files=()):
    rec = session.reconciliation
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if low in CLOSE_WORDS:
        return cancel(ctx)
    balance = _balances(ctx, rec.period_end).get(rec.account.source_id, ZERO)
    intro = _refresh(ctx, rec, balance)
    word, _, rest = low.partition(" ")
    raw_rest = (text or "").strip()[len(word):].strip()
    if files:
        if len(files) > 1:
            raise CommandError("Choose one schedule file.")
        lines = _read_schedule(ctx, files[0], [rec.account])
        intro = _apply_schedule(ctx, rec, lines[rec.account.pk], files[0])
    elif word in ("statement", "support", "balance"):
        if rec.method in AUTO_SUPPORT:
            raise CommandError(f"The supporting balance for {rec.get_method_display().lower()} comes from Xero.")
        rec.support_balance = _money(rest)
        message = f"Supporting balance set to {rec.support_balance:,.2f}."
        if rec.schedule:
            rec.schedule, rec.schedule_source = [], {}
            message += " The schedule was removed; the typed balance replaces it."
        intro = _changed(ctx, rec, message)
    elif word == "add":
        rec.items = rec.items + [_item(raw_rest)]
        intro = _changed(ctx, rec, "Reconciling item added.")
    elif word in ("remove", "delete"):
        numbers = _numbers(rest, len(rec.items))
        if any(rec.items[n - 1]["source"] == "xero" for n in numbers):
            raise CommandError("Items from Xero (not yet reconciled in Xero) can't be removed here: reconcile them in "
                               "Xero, or add an item to offset one.")
        rec.items = [item for i, item in enumerate(rec.items, 1) if i not in numbers]
        intro = _changed(ctx, rec, f"Removed {len(numbers)} item(s).")
    elif word in ("note", "notes"):
        rec.notes = raw_rest
        intro = _changed(ctx, rec, "Note saved." if raw_rest else "Note cleared.", clears=False)
    elif low in ("prepared", "prepare", "sign", "sign prepared"):
        intro = _sign(ctx, rec, "prepared")
    elif low in ("reviewed", "review", "sign reviewed"):
        intro = _sign(ctx, rec, "reviewed")
    elif low == "reopen":
        if not rec.clear_signoff():
            raise CommandError("It isn't signed off, so there's nothing to reopen.")
        rec.save()
        _audit(ctx, rec, "reopened")
        intro = "Reopened: the sign-off is cleared."
    elif low == "next":
        return _next(ctx, rec)
    elif low:
        intro = f'I didn\'t understand "{text.strip()}". {HELP}'
    return show(ctx, rec, balance, intro=intro)


# --- The command ------------------------------------------------------------------------------------------------

def start(ctx, cmd):
    org = ctx.organisation
    if org is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    params = read_params(cmd, ctx, flags={"bank", "template"}, period=True)
    if "template" in params.flags:
        return guide(params.period)
    if "bank" in params.flags:
        if cmd.text or params.period or cmd.input_file:
            raise CommandError("/reconcile --bank shows today's unreconciled Xero items; it takes nothing else.")
        return bank_status(ctx)
    period_end = _period_end(ctx, params.period)
    accounts = _chart(ctx)
    balances = _balances(ctx, period_end)
    account = None
    if cmd.text:
        account = _find_account(accounts, cmd.text)
        if not account.is_balance_sheet:
            raise CommandError(f"{account} is a profit and loss account; /reconcile is for balance sheet accounts.")
    if cmd.input_file:
        return upload_schedules(ctx, ctx.input_file, accounts, balances, period_end, account)
    if account is not None:
        return open_working_paper(ctx, account, period_end, balances.get(account.source_id, ZERO))
    return pack(ctx, accounts, balances, period_end)


def _period_end(ctx, period):
    if period is None:
        return ctx.today.replace(day=1) - timedelta(days=1)
    if period.end > ctx.today:
        raise CommandError(f"{period.label} hasn't ended yet. Reconcile a month that has, e.g. --"
                           f"{_label(ctx.today.replace(day=1) - timedelta(days=1))}.")
    return period.end


def _label(day):
    return f"{day:%b %Y}"


def _chart(ctx):
    """The organisation's active chart of accounts, loading it from Xero the first time."""
    from xero.sync import refresh_accounts

    accounts = list(Account.objects.filter(organisation=ctx.organisation, is_active=True))
    if not accounts:
        refresh_accounts(ctx.organisation, ctx.source)
        accounts = list(Account.objects.filter(organisation=ctx.organisation, is_active=True))
    return accounts


def _balances(ctx, period_end):
    """{Xero account id: balance} from the balance sheet at the period end."""
    return {r["account_id"]: r["amount"] for r in ctx.source.balance_sheet(period_end)
            if r["account_id"] and not r["is_total"]}


def _find_account(accounts, text):
    text = text.strip().lower()
    exact = [a for a in accounts if text in (a.code.lower(), a.name.lower())]
    if len(exact) == 1:
        return exact[0]
    found = [a for a in accounts if text in a.name.lower() and a.is_balance_sheet]
    if len(found) == 1:
        return found[0]
    if not found:
        raise CommandError(f'No account "{text}". Use its code, e.g. /reconcile 090 --Sep 2026.')
    raise CommandError(f'"{text}" matches {len(found)} accounts (' + ", ".join(str(a) for a in found[:6])
                       + "). Use the code.")


# --- The pack ---------------------------------------------------------------------------------------------------

def _row_status(account, balance, rec):
    """(status, support, items total, difference) for an account in the pack."""
    if rec is not None:
        diff = rec.difference
        if rec.reviewed_at:
            status = REVIEWED
        elif rec.prepared_at:
            status = PREPARED
        elif diff is None:
            status = NO_SUPPORT
        elif diff:
            status = DIFFERENCE
        else:
            status = READY
        return status, rec.support_balance, rec.items_total, diff
    method = account.recon_method
    if method == ReconMethod.NIL:
        return (NIL_OK if balance == 0 else DIFFERENCE), ZERO, ZERO, balance
    if balance == 0 and not account.is_bank_account:
        return NIL_OK, None, None, None
    if account.recon_threshold and abs(balance) < account.recon_threshold:
        return BELOW, None, None, None
    return (NOT_SET if not method else NOT_STARTED), None, None, None


def pack(ctx, accounts, balances, period_end):
    recs = {r.account_id: r for r in Reconciliation.objects.filter(organisation=ctx.organisation,
                                                                  period_end=period_end)}
    for account in accounts:  # keep stored Xero balances current so sign-offs reflect today's Xero figures
        rec = recs.get(account.pk)
        if rec is not None:
            _refresh(ctx, rec, balances.get(account.source_id, ZERO))
    known = {a.source_id for a in accounts}
    rows = []
    for account in accounts:
        if not account.is_balance_sheet or account.recon_method == ReconMethod.NONE:
            continue
        balance = balances.get(account.source_id, ZERO)
        rec = recs.get(account.pk)
        if rec is None and balance == 0 and not account.is_bank_account:
            continue
        rows.append((account, balance, rec, *_row_status(account, balance, rec)))
    rows.sort(key=lambda r: (ORDER.index(r[3]), r[0].code))

    result = Result(f"Balance sheet reconciliation at {fmt(period_end)}",
                    subtitle=f"{ctx.settings['name']} · methods and thresholds are set per account in the chart of "
                             "accounts (admin)")
    to_do = [r for r in rows if r[3] not in DONE]
    open_diff = sum((r[6] for r in rows if r[6] and r[3] != PREPARED and r[3] != REVIEWED), ZERO)
    result.add(Figures([
        Figure("Accounts", str(len(rows)), TEXT, detail=f"{sum(1 for r in rows if r[3] == BELOW)} below threshold"),
        Figure("Reviewed", str(sum(1 for r in rows if r[3] in (REVIEWED, NIL_OK))), TEXT,
               detail=f"{sum(1 for r in rows if r[3] == PREPARED)} prepared, awaiting review"),
        Figure("To do", str(len(to_do)), TEXT),
        Figure("Unexplained differences", open_diff),
    ]))
    table = result.add(Table([Column("Code"), Column("Account"), Column("Method"), Column("Xero balance", MONEY),
                              Column("Supporting", MONEY), Column("Reconciling items", MONEY),
                              Column("Difference", MONEY), Column("Status"), Column("Prepared"),
                              Column("Reviewed")]))
    for account, balance, rec, status, support, items, diff in rows:
        prepared = _who(rec.prepared_by, rec.prepared_at) if rec and rec.prepared_at else ""
        reviewed = _who(rec.reviewed_by, rec.reviewed_at) if rec and rec.reviewed_at else ""
        if rec and rec.self_reviewed:
            reviewed += " (self-reviewed)"
        table.add(account.code, account.name, account.get_recon_method_display() or "Not set", balance, support,
                  items, diff, status, prepared, reviewed)
    table.add("", "Total", "", sum((r[1] for r in rows), ZERO), None, None, open_diff, "", "", "", total=True)

    missing = [r for r in ctx.source.balance_sheet(period_end) if r["account_id"] and not r["is_total"]
               and r["account_id"] not in known and r["amount"]]
    if missing:
        result.add(Note("Not in iLA's chart of accounts yet: " + ", ".join(r["label"] for r in missing[:8])
                        + ". Ask an admin to refresh the chart (Admin > Organisations > Refresh chart of accounts).",
                        "warning"))
    if any(r[3] == NOT_SET for r in rows):
        result.add(Note("Some accounts have no reconciliation method: an admin sets it in Admin > Chart of "
                        "accounts.", "warning"))
    first = next((r[0] for r in rows if r[3] not in DONE), None)
    if first is not None:
        result.add(Note(f"Work on an account with /reconcile {first.code} --{_label(period_end)}; type next in a "
                        "working paper to move to the next account to do.", "warning"))
    else:
        result.add(Note("Every account is reconciled and reviewed (or nil, or below its threshold)."))
    return result


def _who(user, when):
    name = (user.get_full_name() or user.get_username()) if user else "?"
    return f"{name} {timezone.localtime(when):%d %b %Y}"


# --- The working paper -----------------------------------------------------------------------------------------

def _check_method(account):
    if not account.recon_method:
        raise CommandError(f"{account} has no reconciliation method yet. An admin sets it in Admin > Chart of "
                           "accounts.")
    if account.recon_method == ReconMethod.NONE:
        raise CommandError(f"{account} is set to Not reconciled in the chart of accounts.")


def _working_paper(ctx, account, period_end, balance):
    """(reconciliation, message): the account's working paper for the period, made (with last period's items
    brought forward) if there isn't one, and up to date with Xero."""
    _check_method(account)
    method = account.recon_method
    intro = ""
    rec = Reconciliation.objects.filter(organisation=ctx.organisation, account=account, period_end=period_end).first()
    if rec is None:
        rec = Reconciliation(organisation=ctx.organisation, account=account, period_end=period_end,
                             method=method, gl_balance=balance)
        carried = _carry_forward(rec)
        rec.save()
        if carried:
            intro = (f"{carried} reconciling item(s) brought forward from the last reconciliation: remove any "
                     "that have cleared.")
    elif rec.method != method:
        rec.method = method
        rec.clear_signoff()
        rec.save()
        intro = "The method changed in the chart of accounts, so the sign-off was cleared."
    refreshed = _refresh(ctx, rec, balance)
    return rec, " ".join(p for p in (intro, refreshed) if p)


def open_working_paper(ctx, account, period_end, balance, *, intro=""):
    with transaction.atomic():
        rec, message = _working_paper(ctx, account, period_end, balance)
        _close(ctx)
        ReconSession.objects.create(organisation=ctx.organisation, user=ctx.user, reconciliation=rec)
    return show(ctx, rec, balance, intro=" ".join(p for p in (intro, message) if p))


def _carry_forward(rec):
    previous = (Reconciliation.objects.filter(organisation=rec.organisation, account=rec.account,
                                              period_end__lt=rec.period_end).order_by("-period_end").first())
    if previous is None:
        return 0
    items = [dict(i, source="carried", carried_from=i.get("carried_from") or previous.period_end.isoformat())
             for i in previous.items if i["source"] in ("manual", "carried")]
    rec.items = items
    if rec.method not in AUTO_SUPPORT and rec.method != ReconMethod.BANK:
        rec.notes = previous.notes
    return len(items)


def _refresh(ctx, rec, balance):
    """Bring the working paper up to date with Xero: the balance, and for automatic methods the supporting
    balance and Xero's items. A change to a signed working paper clears the sign-off. Returns a message or ""."""
    messages = []
    signed = bool(rec.prepared_at)
    before = (rec.gl_balance, rec.support_balance, [i for i in rec.items if i["source"] == "xero"], rec.schedule)
    rec.gl_balance = balance
    if rec.method == ReconMethod.NIL:
        rec.support_balance = ZERO
    elif rec.method == ReconMethod.SUBLEDGER and not signed:
        rec.support_balance = _ledger(ctx, rec)
    elif rec.method in SCHEDULE_METHODS and not signed and (rec.support_balance is None
                                                            or rec.schedule_source.get("ledger")):
        accruals = expectation_ledger_schedule(rec)
        if accruals is not None:
            rec.schedule, rec.schedule_source = accruals
            rec.support_balance = sum((Decimal(line["amount"]) for line in rec.schedule), ZERO)
    if rec.method == ReconMethod.BANK and not signed:
        rec.items = _xero_items(ctx, rec) + [i for i in rec.items if i["source"] != "xero"]  # numbers stay put
    after = (rec.gl_balance, rec.support_balance, [i for i in rec.items if i["source"] == "xero"], rec.schedule)
    if after == before:
        return ""
    if signed and before[0] != rec.gl_balance:
        rec.clear_signoff()
        messages.append(f"The Xero balance changed from {before[0]:,.2f} to {rec.gl_balance:,.2f} since it was signed, "
                        "so the sign-off was cleared.")
        _audit(ctx, rec, "sign-off cleared: Xero balance changed", {"was": str(before[0]), "now": str(rec.gl_balance)})
    rec.save()
    return " ".join(messages)


def _ledger(ctx, rec):
    """Debtors or creditors at the period end from Xero's invoices and bills, or None if Xero couldn't give it."""
    kind = {"DEBTORS": "receivable", "CREDITORS": "payable"}.get(rec.account.system_account)
    if kind is None:
        return None
    try:
        return ctx.source.ledger_balance(kind, rec.period_end)["total"]
    except XeroAPIError:
        return None


def expectation_ledger_schedule(rec):
    """(schedule lines, source) from the Expectation Ledger's accrual journal for the period, if ``rec``'s account is
    the one accrual runs post to; None otherwise or when there's no journal for the period yet. Only lines marked
    posted (or since closed) are in Xero at the period end; the rest are counted in the source as not posted."""
    from accounting.models import Accrual, LedgerJournal, State

    accrual = Accrual.objects.filter(organisation=rec.organisation).first()
    if accrual is None or not accrual.codes or accrual.codes[0] != rec.account.code:
        return None
    journal = LedgerJournal.objects.filter(accrual=accrual, kind=LedgerJournal.Kind.ACCRUAL,
                                           period_end=rec.period_end).first()
    if journal is None:
        return None
    lines, unposted, unposted_total = [], 0, ZERO
    for line in journal.lines.select_related("supplier").order_by("pk"):
        if line.state not in (State.POSTED, State.CLOSED):
            unposted += 1
            unposted_total += line.expected_amount
            continue
        lines.append({"description": line.description[:200],
                      "reference": line.supplier.name[:100] if line.supplier_id else "",
                      "amount": str(line.expected_amount), "from": "", "to": "",
                      "notes": f"{line.get_state_display()} · {line.basis}"[:300]})
    return lines, {"ledger": journal.pk, "journal": str(journal), "rows": len(lines), "unposted": unposted,
                   "unposted_total": str(unposted_total)}


def _xero_items(ctx, rec):
    return [{"description": i["description"], "amount": str(i["amount"]),
             "date": i["date"].isoformat() if i["date"] else "", "source": "xero"}
            for i in ctx.source.unreconciled_items(rec.period_end) if i["account_id"] == rec.account.source_id]


def _changed(ctx, rec, message, *, clears=True):
    if clears and rec.clear_signoff():
        message += " The sign-off was cleared: prepare (and review) it again."
        _audit(ctx, rec, "sign-off cleared: working paper changed")
    rec.save()
    return message


def _sign(ctx, rec, role):
    if role == "prepared":
        if rec.prepared_at:
            raise CommandError("It's already signed as prepared. Type reviewed, or reopen to change it.")
        if rec.difference is None:
            raise CommandError("Enter the supporting balance first (statement 12,345.67 or support 5,000).")
        if rec.difference:
            raise CommandError(f"There's a difference of {rec.difference:,.2f}. Explain it with reconciling items "
                               "(add <amount> <description>) before signing; an unexplained difference can be "
                               "added as an item too, so it's visible.")
        rec.prepared_by, rec.prepared_at = ctx.user, timezone.now()
        message = "Signed as prepared. A reviewer types reviewed."
    else:
        if not rec.prepared_at:
            raise CommandError("It has to be prepared before it's reviewed.")
        if rec.reviewed_at:
            raise CommandError("It's already reviewed. Type reopen to change it.")
        rec.reviewed_by, rec.reviewed_at = ctx.user, timezone.now()
        message = "Reviewed." + (" You also prepared it, so it shows as self-reviewed." if rec.self_reviewed else "")
    rec.save()
    _audit(ctx, rec, role)
    return message


def _audit(ctx, rec, what, extra=None):
    AuditLog.objects.create(organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
                            input=f"/reconcile {rec.account.code} {rec.period_end:%d %b %Y}: {what}"[:2000],
                            target_type="core.reconciliation", target_id=str(rec.pk),
                            result={"gl_balance": str(rec.gl_balance), "support": str(rec.support_balance),
                                    "items": str(rec.items_total), **(extra or {})})


def _next(ctx, rec):
    accounts = _chart(ctx)
    balances = _balances(ctx, rec.period_end)
    recs = {r.account_id: r for r in Reconciliation.objects.filter(organisation=ctx.organisation,
                                                                  period_end=rec.period_end)}
    todo = []
    for account in accounts:
        if not account.is_balance_sheet or account.recon_method in ("", ReconMethod.NONE):
            continue
        balance = balances.get(account.source_id, ZERO)
        status = _row_status(account, balance, recs.get(account.pk))[0]
        if status not in DONE and status != PREPARED and account.pk != rec.account_id:
            todo.append((ORDER.index(status), account.code, account, balance))
    if not todo:
        _close(ctx)
        return Result("Nothing left to do", subtitle=f"Every account at {fmt(rec.period_end)} is prepared, reviewed, "
                                                     "nil or below its threshold. See /reconcile --"
                                                     f"{_label(rec.period_end)}.")
    _, _, account, balance = min(todo, key=lambda t: (t[0], t[1]))
    return open_working_paper(ctx, account, rec.period_end, balance)


def show(ctx, rec, balance, *, intro=""):
    account = rec.account
    result = Result(f"{account} at {fmt(rec.period_end)}",
                    subtitle=f"{rec.get_method_display()} · working paper open: type here to change it")
    if intro:
        result.add(Note(intro))
    support_label = SUPPORT_LABEL.get(rec.method, "Supporting balance")
    diff = rec.difference
    result.add(Figures([
        Figure("Xero balance", rec.gl_balance),
        Figure(support_label, rec.support_balance if rec.support_balance is not None else "Not entered",
               MONEY if rec.support_balance is not None else TEXT),
        Figure("Reconciling items", rec.items_total, detail=f"{len(rec.items)} item(s)"),
        Figure("Difference", diff if diff is not None else "–", MONEY if diff is not None else TEXT,
               detail="must be nil to sign" if diff else ""),
    ]))
    if rec.items:
        table = result.add(Table([Column("#", NUMBER), Column("Date", DATE), Column("Description"), Column("From"),
                                  Column("Amount", MONEY)], title="Reconciling items"))
        for n, item in enumerate(rec.items, 1):
            source = {"xero": "Xero (unreconciled)", "manual": "Added",
                      "carried": f"Brought forward from {item.get('carried_from', '')}"}.get(item["source"], "")
            table.add(n, date.fromisoformat(item["date"]) if item.get("date") else None, item["description"], source,
                      Decimal(item["amount"]))
        table.add(None, None, "Total", "", rec.items_total, total=True)
    if rec.schedule or rec.schedule_source.get("ledger"):
        _schedule_table(result, rec)
    elif rec.method in SCHEDULE_METHODS and rec.support_balance is None:
        result.add(Note(f"Upload the schedule with /reconcile {account.code} --{_label(rec.period_end)} << "
                        "schedule.csv (or type << schedule.csv here), or type support <amount>. /reconcile --template "
                        "has the template.", "warning"))
    if rec.notes:
        result.add(Note(f"Note: {rec.notes}"))
    if rec.method == ReconMethod.BANK:
        result.add(Note(BANK_NOTE))
    if rec.method == ReconMethod.SUBLEDGER and rec.support_balance is None:
        result.add(Note("Xero's invoices and bills couldn't give the ledger at this date; check the connection's "
                        "permissions with /connect.", "warning"))
    if rec.reviewed_at:
        signed = f"Prepared by {_who(rec.prepared_by, rec.prepared_at)}; reviewed by {_who(rec.reviewed_by, rec.reviewed_at)}"
        result.add(Note(signed + (" (self-reviewed)." if rec.self_reviewed else ".")))
    elif rec.prepared_at:
        result.add(Note(f"Prepared by {_who(rec.prepared_by, rec.prepared_at)}; awaiting review (type reviewed)."))
    result.add(Note(HELP, "warning"))
    return result


# --- Schedules --------------------------------------------------------------------------------------------------

def guide(period):
    when = period.label if period else "Sep 2026"
    result = Result("Reconciliation schedules", subtitle="The lines making up an account's supporting balance")
    result.link, result.link_text = reverse("console:reconcile_template"), "Download the template (schedule-template.csv)"
    table = result.add(Table([Column("Column"), Column("What goes in it")], title="Filling in the template"))
    for row in GUIDE_ROWS:
        table.add(*row)
    result.add(Note("One file can hold several accounts' schedules (one or more rows each). Accounts must use the "
                    "Schedule, Fixed asset register, GST return or Intercompany method in the chart of accounts."))
    result.add(Note(f"Next: /reconcile --{when} << schedules.csv for several accounts, or /reconcile 1400 --{when} << "
                    "prepayments.csv for one. Each account's working paper then shows its schedule; anything signed "
                    "is reopened.", "warning"))
    return result


def template_csv():
    out = io.StringIO()
    csv.writer(out).writerows(TEMPLATE_ROWS)
    return "\ufeff" + out.getvalue()


def _read_schedule(ctx, upload, accounts):
    """{account pk: [line, ...]} from a schedule CSV, every problem reported together. ``accounts`` are the ones the
    file may name; with one account given in the command the Account column may be left out or empty."""
    if not upload.name.lower().endswith(".csv"):
        raise CommandError(f"{upload.name} isn't a CSV file. Save it as CSV (in Excel: File → Save As → CSV UTF-8).")
    if upload.size > MAX_FILE_BYTES:
        raise CommandError(f"{upload.name} is over 2 MB.")
    raw = upload.read()
    upload.seek(0)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    rows = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    if len(rows) < 2:
        raise CommandError(f"{upload.name} needs a heading row and at least one line.")
    index = {h.strip().lower(): i for i, h in enumerate(rows[0]) if h.strip()}
    for required in ("description", "amount"):
        if required not in index:
            raise CommandError(f'{upload.name} needs a "{required.title()}" column. /reconcile --template has the '
                               "template.")
    only = accounts[0] if len(accounts) == 1 else None
    if "account" not in index and only is None:
        raise CommandError(f'{upload.name} needs an "Account" column, or name the account in the command: '
                           "/reconcile 1400 --Sep 2026 << schedule.csv.")

    def cell(row, name):
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    by_code = {a.code.lower(): a for a in accounts if a.code}
    lines, problems = {}, []
    for n, row in enumerate(rows[1:], 2):
        try:
            code = cell(row, "account")
            if code:
                account = by_code.get(code.lower())
                if account is None:
                    raise CommandError(f"account {code} " + (f"isn't {only}." if only else
                                                             "isn't a balance sheet account in the chart."))
            elif only is not None:
                account = only
            else:
                raise CommandError("Account is empty.")
            if account.recon_method not in SCHEDULE_METHODS:
                raise CommandError(f"{account} is reconciled by {account.get_recon_method_display() or 'no method'}, "
                                   "not a schedule (change it in Admin > Chart of accounts).")
            description = cell(row, "description")
            if not description:
                raise CommandError("Description is empty.")
            amount = _money(cell(row, "amount"))
            start_text, end_text = cell(row, "from"), cell(row, "to")
            start_, end_ = parse_date(start_text) if start_text else None, parse_date(end_text) if end_text else None
            if (start_text and not start_) or (end_text and not end_):
                raise CommandError("From and To must be dates, e.g. 30/06/2027.")
            if start_ and end_ and end_ < start_:
                raise CommandError("To is before From.")
            lines.setdefault(account.pk, []).append({
                "description": description[:200], "reference": cell(row, "reference")[:100], "amount": str(amount),
                "from": start_.isoformat() if start_ else "", "to": end_.isoformat() if end_ else "",
                "notes": cell(row, "notes")[:300]})
        except CommandError as exc:
            problems.append(f"row {n}: {exc}")
    if problems:
        raise CommandError(f"{upload.name} needs fixing; nothing was saved. " + " ".join(problems[:12])
                           + (f" (and {len(problems) - 12} more)" if len(problems) > 12 else ""))
    return lines


def _apply_schedule(ctx, rec, lines, upload):
    """Make ``lines`` the working paper's schedule and its total the supporting balance. Returns a message."""
    raw = upload.read()
    upload.seek(0)
    rec.schedule = lines
    rec.support_balance = sum((Decimal(line["amount"]) for line in lines), ZERO)
    rec.schedule_source = {"file": upload.name[:200], "sha256": hashlib.sha256(raw).hexdigest(), "rows": len(lines),
                           "by": ctx.user.get_username() if ctx.user else "",
                           "at": timezone.now().isoformat(timespec="seconds")}
    message = _changed(ctx, rec, f"Schedule from {upload.name}: {len(lines)} line(s), {rec.support_balance:,.2f}.")
    _audit(ctx, rec, "schedule uploaded", {"file": rec.schedule_source["file"], "sha256": rec.schedule_source["sha256"],
                                          "rows": len(lines)})
    return message


def upload_schedules(ctx, upload, accounts, balances, period_end, account=None):
    """``/reconcile [<account>] --<period> << schedule.csv``: the file's schedules onto the period's working papers,
    all or nothing. With one account, its working paper opens; with several, a summary."""
    if account is not None:
        _check_method(account)
    allowed = [account] if account is not None else [a for a in accounts if a.is_balance_sheet]
    lines = _read_schedule(ctx, upload, allowed)
    targets = {a.pk: a for a in allowed if a.pk in lines}
    if account is not None:
        with transaction.atomic():
            rec, message = _working_paper(ctx, account, period_end, balances.get(account.source_id, ZERO))
            done = _apply_schedule(ctx, rec, lines[account.pk], upload)
            _close(ctx)
            ReconSession.objects.create(organisation=ctx.organisation, user=ctx.user, reconciliation=rec)
        return show(ctx, rec, rec.gl_balance, intro=" ".join(p for p in (message, done) if p))
    result = Result(f"Schedules from {upload.name} at {fmt(period_end)}",
                    subtitle=f"{len(targets)} account(s); open one to sign it off")
    table = result.add(Table([Column("Code"), Column("Account"), Column("Lines", NUMBER), Column("Schedule", MONEY),
                              Column("Xero balance", MONEY), Column("Reconciling items", MONEY),
                              Column("Difference", MONEY)]))
    with transaction.atomic():
        for pk, target in sorted(targets.items(), key=lambda t: t[1].code):
            rec, _ = _working_paper(ctx, target, period_end, balances.get(target.source_id, ZERO))
            _apply_schedule(ctx, rec, lines[pk], upload)
            table.add(target.code, target.name, len(lines[pk]), rec.support_balance, rec.gl_balance,
                      rec.items_total, rec.difference)
    first = min(targets.values(), key=lambda a: a.code)
    result.add(Note(f"Open one with /reconcile {first.code} --{_label(period_end)} to see its lines and sign it. "
                    "Anything already signed for these accounts was reopened.", "warning"))
    return result


def _schedule_table(result, rec):
    source = rec.schedule_source or {}
    title = "Schedule" + (f" from {source['file']}" if source.get("file") else "")
    if source.get("ledger"):
        title = f"Schedule from the Expectation Ledger: {source['journal']}"
    table = result.add(Table([Column("#", NUMBER), Column("Description"), Column("Reference"), Column("From", DATE),
                              Column("To", DATE), Column("Notes"), Column("Amount", MONEY)], title=title))
    ended = []
    for n, line in enumerate(rec.schedule, 1):
        end_ = date.fromisoformat(line["to"]) if line.get("to") else None
        if end_ and end_ <= rec.period_end and Decimal(line["amount"]):
            ended.append(f"{n} {line['description']}")
        table.add(n, line["description"], line.get("reference", ""),
                  date.fromisoformat(line["from"]) if line.get("from") else None, end_, line.get("notes", ""),
                  Decimal(line["amount"]))
    table.add(None, "Total", "", None, None, "", rec.support_balance, total=True)
    if ended:
        result.add(Note(f"Line(s) whose period ended by {fmt(rec.period_end)} still have a balance: "
                        + "; ".join(ended[:6]) + ". Check they should still be in the schedule.", "warning"))
    if source.get("by"):
        result.add(Note(f"Uploaded by {source['by']} on {source['at'][:10]}; SHA-256 {source['sha256'][:12]}…"))
    if source.get("ledger"):
        result.add(Note("The posted (and since closed) lines of this month's accrual journal in the Expectation "
                        "Ledger (Admin > Accounting). Upload a schedule or type support to use something else."))
        if source.get("unposted"):
            result.add(Note(f"{source['unposted']} more line(s) in that journal, {Decimal(source['unposted_total']):,.2f}, "
                            "aren't marked posted, so they aren't counted. If they're in Xero, mark the journal "
                            "posted in Admin > Accounting > Expectation Ledger.", "warning"))


# --- Parsing --------------------------------------------------------------------------------------------------

def _money(text):
    cleaned = text.replace(",", "").replace("$", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned).quantize(CENT)
    except (InvalidOperation, ValueError):
        raise CommandError(f'"{text}" isn\'t an amount, e.g. 12,345.67 or -250.') from None
    return -value if negative else value


def _item(text):
    """"[date] amount description" -> a manual reconciling item."""
    parts = text.split()
    day = None
    if parts and parse_date(parts[0]):
        day = parse_date(parts.pop(0))
    if len(parts) < 2:
        raise CommandError("Give the amount and a description, e.g. add -250.00 Unpresented cheque 123.")
    amount = _money(parts[0])
    return {"description": " ".join(parts[1:])[:200], "amount": str(amount),
            "date": day.isoformat() if day else "", "source": "manual"}


def _numbers(text, count):
    numbers = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise CommandError(f'"{part}" isn\'t an item number, e.g. remove 2 or remove 1-3.')
        low, high = int(m.group(1)), int(m.group(2) or m.group(1))
        numbers.update(range(min(low, high), max(low, high) + 1))
    if not numbers or min(numbers) < 1 or max(numbers) > count:
        raise CommandError(f"Choose item numbers from 1 to {count}." if count else "There are no items to remove.")
    return numbers


# --- Bank status (what /recon showed) ---------------------------------------------------------------------------

RECON_NOTE = (
    "These are transactions and payments entered in Xero but not yet reconciled. Xero's dashboard count is "
    "different: it counts bank feed statement lines still waiting to be matched, and Xero only shares those "
    "through a restricted API that needs Xero's approval. Check Xero's reconcile screen for that number."
)


def recon_table(unreconciled):
    table = Table([Column("Bank account"), Column("Unreconciled items", NUMBER), Column("Net amount", MONEY),
                   Column("Oldest unreconciled", DATE)], title="Bank reconciliation")
    for u in sorted(unreconciled, key=lambda u: u["account"]):
        table.add(u["account"], u["count"], u["amount"], u["oldest"])
    return table


def bank_status(ctx):
    result = Result(f"Bank reconciliation at {fmt(ctx.today)}", subtitle=ctx.settings["name"])
    table = result.add(recon_table(ctx.source.unreconciled()))
    table.title = ""
    result.add(Note(RECON_NOTE))
    return result
