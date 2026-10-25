"""The console commands. Each takes (ctx, cmd) and returns a Result; figures come from ``ctx.source``."""

import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q
from django.urls import reverse

from core.models import Dimension, Organisation
from xero.api import XeroAPIError, XeroScopeMissing

from .builder import active_commands
from .output import DATE, MONEY, NUMBER, PCT, TEXT, Column, Figure, Figures, Note, Result, Table
from .output import fmt_money as _m
from .periods import Period, add_months, fmt, month_period, window
from .reconcile import RECON_NOTE
from .reconcile import recon_table as _recon_table
from .registry import COMMANDS, CommandError, as_at_date, command, lookup, positive_int, read_params

ZERO = Decimal("0")
WINDOWS = ("mtd", "qtd", "ytd")
# flag -> (label, ProfitAndLoss measure, higher is favourable)
MEASURES = {
    "revenue": ("Revenue", "revenue", True),
    "cos": ("Cost of sales", "cost_of_sales", False),
    "gp": ("Gross profit", "gross_profit", True),
    "expenses": ("Expenses", "expenses", False),
    "profit": ("Net profit", "net_profit", True),
}
MEASURE_ALIASES = {"sales": "revenue", "income": "revenue", "cost of sales": "cos", "gross profit": "gp",
                   "costs": "expenses", "net profit": "profit"}
PARTIAL_BUDGET_NOTE = "Budgets for part-months are pro-rated by calendar days."


def variance(actual, compare, higher_is_good):
    """Favourable/(unfavourable) amount and percentage of ``compare``."""
    if compare is None:
        return None, None
    diff = actual - compare if higher_is_good else compare - actual
    return diff, (diff / abs(compare) if compare else None)


def _measure_flags(params):
    chosen = []
    for flag in params.flags:
        key = MEASURE_ALIASES.get(flag, flag)
        if key in MEASURES and key not in chosen:
            chosen.append(key)
    return [m for m in MEASURES if m in chosen]


def _periods(params, ctx):
    """The periods to report: one per --mtd/--qtd/--ytd (to the period's end, or today), else the period itself,
    else month to date. Periods running into the future stop at today."""
    windows = [w for w in WINDOWS if w in params.flags]
    if params.period:
        base = params.period.capped(ctx.today)
    else:
        base = None
    if not windows:
        return [base] if base else [window("mtd", ctx.today, ctx.fy)]
    reference = base.end if base else ctx.today
    return [window(w, reference, ctx.fy) for w in windows]


def _is_partial_month(period):
    nxt = period.end + timedelta(days=1)
    return period.start.day != 1 or nxt.day != 1


# --- /status ---------------------------------------------------------------------------------------------------


@command(
    "status",
    summary="Quick update: cash, revenue, debtors, bills due and bank reconciliation; or a measure against budget "
            "and last year.",
    usage="/status [--<period>] [--revenue|--cos|--gp|--expenses|--profit] [--mtd|--qtd|--ytd]",
    params=[
        ("--<period>", "September 2026, Sep 2026, 2026-09, last month, Q1 FY2027, FY2026, Jul 2026 to Sep 2026"),
        ("--revenue --cos --gp --expenses --profit", "Measures to show (all four main ones if none given)"),
        ("--mtd --qtd --ytd", "Month, quarter or financial year to date, ending at the period end (or today)"),
    ],
    examples=["/status", "/status --September 2026 --revenue --mtd", "/status --revenue --mtd --ytd",
              "/status --FY2026 --profit"],
)
def status(ctx, cmd):
    params = read_params(cmd, ctx, flags=set(MEASURES) | set(MEASURE_ALIASES) | set(WINDOWS), period=True)
    if not params.flags and not params.period:
        return _snapshot(ctx)
    return _measures(ctx, params)


def _snapshot(ctx):
    src, today = ctx.source, ctx.today
    result = Result(f"Status at {fmt(today)}", subtitle=ctx.settings["name"])
    figures = result.add(Figures([]))
    tables, notes = [], []

    try:
        banks = src.bank_balances(today)
        figures.items.append(Figure("Cash at bank", sum((b["balance"] for b in banks), ZERO),
                                    detail=f"{len(banks)} bank account{'s' if len(banks) != 1 else ''}"))
        table = Table([Column("Bank account"), Column("Balance", MONEY)], title="Cash at bank")
        for b in banks:
            table.add(b["account"], b["balance"])
        tables.append(table)
    except XeroScopeMissing as exc:
        notes.append(Note(f"Cash at bank: {exc}", "warning"))

    revenue_rows = Table([Column("Revenue"), Column("Actual", MONEY), Column("Budget", MONEY),
                          Column("Fav/(Unfav)", MONEY), Column("Last year", MONEY), Column("Fav/(Unfav)", MONEY)],
                         title="Revenue")
    for kind in ("mtd", "ytd"):
        period = window(kind, today, ctx.fy)
        current, last_year = src.profit_and_loss(period.start, period.end, with_last_year=True)
        budget = src.budget(period.start, period.end)
        actual = current.measure("revenue")
        bud = budget.measure("revenue") if budget else None
        ly = last_year.measure("revenue")
        detail = []
        if bud is not None:
            detail.append(f"budget {_m(bud)}")
        detail.append(f"last year {_m(ly)}")
        figures.items.append(Figure(f"Revenue {kind.upper()}", actual, detail=", ".join(detail)))
        revenue_rows.add(period.label, actual, bud, variance(actual, bud, True)[0], ly, variance(actual, ly, True)[0])
    tables.append(revenue_rows)

    try:
        figures.items.append(Figure("Debtors", src.system_balance(today, "DEBTORS"), detail="per balance sheet"))
    except XeroScopeMissing as exc:
        notes.append(Note(f"Debtors: {exc}", "warning"))

    horizon = today + timedelta(days=7)
    bills = src.open_documents("payable", due_by=horizon)
    overdue = [b for b in bills if b["due_date"] and b["due_date"] < today]
    overdue_total = sum((b["amount_due_base"] for b in overdue), ZERO)
    detail = f"{len(bills)} bill{'s' if len(bills) != 1 else ''}"
    if overdue:
        detail += f", including {_m(overdue_total)} overdue"
        notes.append(Note(f"Bills due by {fmt(horizon)} include {_m(overdue_total)} on {len(overdue)} "
                          f"bill{'s' if len(overdue) != 1 else ''} due before {fmt(today)}, already overdue. "
                          f"Use /bills --overdue to list them.", "warning"))
    figures.items.append(Figure(
        f"Bills due by {fmt(horizon)}", sum((b["amount_due_base"] for b in bills), ZERO), detail=detail,
    ))

    unreconciled = src.unreconciled()
    items = sum(u["count"] for u in unreconciled)
    oldest = min((u["oldest"] for u in unreconciled if u["oldest"]), default=None)
    figures.items.append(Figure(
        "Unreconciled Xero transactions", f"{items}" if items else "None", TEXT,
        detail=(f"oldest {fmt(oldest)}; " if oldest else "") + "bank feed lines not included",
    ))
    tables.append(_recon_table(unreconciled))

    for block in tables + notes:
        result.add(block)
    result.add(Note(RECON_NOTE))
    if not src.client.has_scope("accounting.budgets.read"):
        result.add(Note("Budgets need permission: reconnect with /connect to compare against budget.", "warning"))
    return result


def _measures(ctx, params):
    src = ctx.source
    measures = _measure_flags(params) or ["revenue", "gp", "expenses", "profit"]
    periods = _periods(params, ctx)
    result = Result("Performance against budget and last year", subtitle=ctx.settings["name"])
    table = result.add(Table([
        Column("Measure"), Column("Period"), Column("Actual", MONEY), Column("Budget", MONEY),
        Column("Fav/(Unfav)", MONEY), Column("%", PCT), Column("Last year", MONEY), Column("Fav/(Unfav)", MONEY),
        Column("%", PCT),
    ]))
    budget_missing = partial = False
    for period in periods:
        current, last_year = src.profit_and_loss(period.start, period.end, with_last_year=True)
        budget = src.budget(period.start, period.end)
        budget_missing |= budget is None
        partial |= _is_partial_month(period)
        for key in measures:
            label, measure, higher_is_good = MEASURES[key]
            actual = current.measure(measure)
            bud = budget.measure(measure) if budget else None
            ly = last_year.measure(measure)
            table.add(label, period.label, actual, bud, *variance(actual, bud, higher_is_good), ly,
                      *variance(actual, ly, higher_is_good))
    if budget_missing:
        result.add(Note(_budget_missing_reason(src), "warning"))
    elif partial:
        result.add(Note(PARTIAL_BUDGET_NOTE))
    return result


def _budget_missing_reason(src):
    if not src.client.has_scope("accounting.budgets.read"):
        return "No budget column: iLA needs permission to read budgets. Reconnect with /connect."
    return "No budget column: the organisation has no overall budget in Xero."


# --- /pl -------------------------------------------------------------------------------------------------------


@command(
    "pl",
    summary="Profit and loss by account, against budget and last year; or by event (any iLA dimension) with "
            "Unassigned and a check against Xero.",
    usage="/pl [--<period>] [--mtd|--qtd|--ytd] [--by <dimension> | --<dimension>] [>> file.xlsx]",
    params=[("--<period>", "Defaults to this month to date"), ("--mtd --qtd --ytd", "To-date window (pick one)"),
            ("--by <dimension>  or  --<dimension>", "A column per value, e.g. --by event or just --event. Xero "
                                                    "tracking and iLA tags count together.")],
    examples=["/pl", "/pl --September 2026", "/pl --ytd >> pl-ytd.xlsx", "/pl --Jul 2026 to Sep 2026",
              "/pl --event --Sep 2026", "/pl --by event --FY2027 >> events.xlsx"],
    aliases=("p&l", "profit"),
)
def pl(ctx, cmd):
    dimensions = {}
    if isinstance(ctx.organisation, Organisation):
        for d in Dimension.objects.filter(organisation=ctx.organisation, is_active=True):
            dimensions.setdefault(d.code.lower(), d)
            dimensions.setdefault(d.name.lower(), d)
    if cmd.text.startswith("-"):
        raise CommandError(f'Use two dashes: /pl --{cmd.text.lstrip("-")}')
    params = read_params(cmd, ctx, flags=set(WINDOWS) | (set(dimensions) - set(WINDOWS)), period=True,
                         exclusive=[WINDOWS], keyed={"by": lambda value, _: value.strip()})
    chosen = [dimensions[f] for f in params.flags if f in dimensions]
    if "by" in params.values:
        wanted = params.values["by"].lower()
        if wanted not in dimensions:
            raise CommandError(f'No iLA dimension called "{params.values["by"]}". Yours: '
                               + (", ".join(sorted({d.name for d in dimensions.values()})) or "none yet (see "
                                  "/createTag)") + ".")
        chosen.append(dimensions[wanted])
    if len({d.pk for d in chosen}) > 1:
        raise CommandError("Report by one dimension at a time.")
    period = _periods(params, ctx)[0]
    if chosen:
        from .pl_by_dimension import report

        return report(ctx, chosen[0], period)
    src = ctx.source
    accounts = src.accounts()
    current, last_year = src.profit_and_loss(period.start, period.end, with_last_year=True)
    budget = src.budget(period.start, period.end)

    result = Result(f"Profit and loss: {period.label}", subtitle=ctx.settings["name"])
    table = result.add(Table([
        Column("Code"), Column("Account"), Column("Actual", MONEY), Column("Budget", MONEY),
        Column("Fav/(Unfav)", MONEY), Column("Last year", MONEY), Column("Fav/(Unfav)", MONEY),
    ]))
    sections = [("revenue", "Revenue", True), ("other_income", "Other income", True),
                ("cost_of_sales", "Cost of sales", False), ("expenses", "Expenses", False)]
    totals = {}
    for category, title, good_high in sections:
        ids = {a for pl_ in (current, last_year, budget) if pl_ for a, c in pl_.category_of.items() if c == category}
        if not ids and category == "other_income":
            continue
        table.add("", title, None, None, None, None, None, total=True)
        for account_id in sorted(ids, key=lambda a: (accounts.get(a, {}).get("code", ""), _name(a, accounts, current))):
            act = current.by_account.get(account_id, ZERO)
            bud = budget.by_account.get(account_id, ZERO) if budget else None
            ly = last_year.by_account.get(account_id, ZERO)
            table.add(accounts.get(account_id, {}).get("code", ""), _name(account_id, accounts, current, last_year),
                      act, bud, variance(act, bud, good_high)[0], ly, variance(act, ly, good_high)[0])
        totals[category] = (current.total(category), budget.total(category) if budget else None,
                            last_year.total(category))
        act, bud, ly = totals[category]
        table.add("", f"Total {title.lower()}", act, bud, variance(act, bud, good_high)[0], ly,
                  variance(act, ly, good_high)[0], total=True)
        if category == "cost_of_sales":
            _add_measure_row(table, "Gross profit", "gross_profit", current, budget, last_year, True)

    if current.unattributed or last_year.unattributed:
        act, ly = -current.unattributed, -last_year.unattributed
        table.add("", "Other items not linked to an account (e.g. currency gains/losses)", act, None, None, ly,
                  variance(act, ly, False)[0])
    _add_measure_row(table, "Net profit", "net_profit", current, budget, last_year, True)

    if budget is None:
        result.add(Note(_budget_missing_reason(src), "warning"))
    elif _is_partial_month(period):
        result.add(Note(PARTIAL_BUDGET_NOTE))
    return result


def _name(account_id, accounts, *pls):
    if account_id in accounts:
        return accounts[account_id]["name"]
    return next((p.names[account_id] for p in pls if p and account_id in p.names), account_id)


def _add_measure_row(table, label, measure, current, budget, last_year, good_high):
    act = current.measure(measure)
    bud = budget.measure(measure) if budget else None
    ly = last_year.measure(measure)
    table.add("", label, act, bud, variance(act, bud, good_high)[0], ly, variance(act, ly, good_high)[0], total=True)


# --- Balance sheet, trial balance, cash ------------------------------------------------------------------------


def _as_at(cmd, ctx, extra_flags=()):
    params = read_params(cmd, ctx, flags=set(extra_flags), keyed={"as at": as_at_date}, period=True)
    if params.period and "as at" in params.values:
        raise CommandError("Give either a date or --as at, not both.")
    day = params.values.get("as at") or (params.period.end if params.period else ctx.today)
    return day, params


@command(
    "bs",
    summary="Balance sheet as at a date (default today).",
    usage="/bs [--as at <date>] [>> file.xlsx]",
    params=[("--as at <date>", "30/06/2026, 30 Jun 2026, FY2026 (its last day), last month (its last day)")],
    examples=["/bs", "/bs --as at 30/06/2026", "/bs --FY2026 >> bs-fy26.xlsx"],
    aliases=("balancesheet",),
)
def bs(ctx, cmd):
    day, _ = _as_at(cmd, ctx)
    rows = ctx.source.balance_sheet(day)
    result = Result(f"Balance sheet as at {fmt(day)}", subtitle=ctx.settings["name"])
    table = result.add(Table([Column("Section"), Column("Account"), Column("Amount", MONEY)]))
    for r in rows:
        table.add(r["section"], r["label"], r["amount"], total=r["is_total"] or not r["account_id"])
    return result


@command(
    "tb",
    summary="Trial balance as at a date (default today).",
    usage="/tb [--as at <date>] [>> file.xlsx]",
    params=[("--as at <date>", "30/06/2026, 30 Jun 2026, FY2026, last month")],
    examples=["/tb", "/tb --as at 30/09/2026 >> tb-sep.xlsx"],
)
def tb(ctx, cmd):
    day, _ = _as_at(cmd, ctx)
    header, rows = ctx.source.trial_balance(day)
    result = Result(f"Trial balance as at {fmt(day)}", subtitle=ctx.settings["name"])
    labels = header or ["Debit", "Credit", "YTD Debit", "YTD Credit"]
    table = result.add(Table([Column("Section"), Column("Account")] + [Column(h, MONEY) for h in labels]))
    for r in rows:
        values = (r["values"] + [None] * len(labels))[:len(labels)]
        table.add(r["section"], r["label"], *values, total=r["is_total"])
    return result


@command(
    "cash",
    summary="Bank account balances as at a date (default today).",
    usage="/cash [--as at <date>]",
    params=[("--as at <date>", "30/06/2026, last month")],
    examples=["/cash", "/cash --as at 30/09/2026"],
    aliases=("bank",),
)
def cash(ctx, cmd):
    day, _ = _as_at(cmd, ctx)
    banks = ctx.source.bank_balances(day)
    result = Result(f"Cash at bank as at {fmt(day)}", subtitle=ctx.settings["name"])
    table = result.add(Table([Column("Bank account"), Column("Balance", MONEY)]))
    for b in banks:
        table.add(b["account"], b["balance"])
    table.add("Total", sum((b["balance"] for b in banks), ZERO), total=True)
    return result


# --- Debtors, creditors, bills ---------------------------------------------------------------------------------

BUCKETS = (("Current", None, 0), ("1-30", 1, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None))


def _bucket(days_overdue):
    for label, low, high in BUCKETS:
        if (low is None or days_overdue >= low) and (high is None or days_overdue <= high):
            return label
    return BUCKETS[0][0]


def _ageing(ctx, cmd, ledger, title, system_account):
    params = read_params(cmd, ctx, flags={"detail"}, keyed={"top": positive_int})
    docs = ctx.source.open_documents(ledger)
    today = ctx.today
    result = Result(f"{title} ageing at {fmt(today)}", subtitle=ctx.settings["name"])

    if "detail" in params.flags:
        table = result.add(Table([Column("Contact"), Column("Number"), Column("Date", DATE), Column("Due", DATE),
                                  Column("Days overdue", NUMBER), Column("Currency"), Column("Amount due", MONEY)]))
        for d in sorted(docs, key=lambda d: (d["contact"].lower(), d["due_date"] or today)):
            overdue = max((today - d["due_date"]).days, 0) if d["due_date"] else 0
            table.add(d["contact"], d["number"], d["date"], d["due_date"], overdue, d["currency"], d["amount_due_base"])
        table.add("Total", "", None, None, None, "", sum((d["amount_due_base"] for d in docs), ZERO), total=True)
    else:
        by_contact = {}
        for d in docs:
            overdue = (today - d["due_date"]).days if d["due_date"] else 0
            row = by_contact.setdefault(d["contact"], {label: ZERO for label, _, _ in BUCKETS})
            row[_bucket(overdue)] += d["amount_due_base"]
        ordered = sorted(by_contact.items(), key=lambda kv: -sum(kv[1].values()))
        if "top" in params.values:
            ordered = ordered[:params.values["top"]]
        table = result.add(Table([Column("Contact")] + [Column(label, MONEY) for label, _, _ in BUCKETS]
                                 + [Column("Total", MONEY)]))
        for contact, row in ordered:
            table.add(contact, *row.values(), sum(row.values()))
        totals = [sum((r[label] for _, r in by_contact.items()), ZERO) for label, _, _ in BUCKETS]
        table.add("Total" + (" (all contacts)" if "top" in params.values else ""), *totals, sum(totals), total=True)

    open_total = sum((d["amount_due_base"] for d in docs), ZERO)
    try:
        ledger_total = ctx.source.system_balance(today, system_account)
        diff = ledger_total - open_total
        if diff:
            result.add(Note(f"Balance sheet {title.lower()} is {_m(ledger_total)}; the {_m(diff)} difference is "
                            "unallocated credit notes, overpayments and prepayments, or foreign currency "
                            "documents valued at today's rate in the ledger."))
    except XeroScopeMissing:
        pass
    if any(d["currency"] and d["currency"] != ctx.settings["base_currency"] for d in docs):
        result.add(Note("Foreign currency documents are converted at their own exchange rate."))
    result.subtitle = f"{ctx.settings['name']} · open total {_m(open_total)}"
    return result


@command(
    "debtors",
    summary="Receivables ageing by customer (by days past due date).",
    usage="/debtors [--detail] [--top <n>] [>> file.xlsx]",
    params=[("--detail", "List each invoice instead of totals per customer"),
            ("--top <n>", "Only the n largest customers")],
    examples=["/debtors", "/debtors --top 10", "/debtors --detail >> debtors.xlsx"],
    aliases=("ar",),
)
def debtors(ctx, cmd):
    return _ageing(ctx, cmd, "receivable", "Debtors", "DEBTORS")


@command(
    "creditors",
    summary="Payables ageing by supplier (by days past due date).",
    usage="/creditors [--detail] [--top <n>] [>> file.xlsx]",
    params=[("--detail", "List each bill instead of totals per supplier"),
            ("--top <n>", "Only the n largest suppliers")],
    examples=["/creditors", "/creditors --detail >> creditors.xlsx"],
    aliases=("ap",),
)
def creditors(ctx, cmd):
    return _ageing(ctx, cmd, "payable", "Creditors", "CREDITORS")


@command(
    "bills",
    summary="Bills due in the next 7 days (or --due <days>), with overdue bills listed first.",
    usage="/bills [--due <days>] [--overdue] [>> file.xlsx]",
    params=[("--due <days>", "Look ahead this many days (default 7)"), ("--overdue", "Only bills already overdue")],
    examples=["/bills", "/bills --due 14", "/bills --overdue >> overdue-bills.csv"],
)
def bills(ctx, cmd):
    params = read_params(cmd, ctx, flags={"overdue"}, keyed={"due": positive_int})
    today = ctx.today
    horizon = today - timedelta(days=1) if "overdue" in params.flags else today + timedelta(days=params.values.get("due", 7))
    docs = sorted(ctx.source.open_documents("payable", due_by=horizon), key=lambda d: (d["due_date"] or today, d["contact"]))
    title = "Overdue bills" if "overdue" in params.flags else f"Bills due by {fmt(horizon)}"
    result = Result(title, subtitle=ctx.settings["name"])
    table = result.add(Table([Column("Due", DATE), Column("Supplier"), Column("Number"), Column("Status"),
                              Column("Currency"), Column("Amount due", MONEY)]))
    for d in docs:
        state = "Overdue" if d["due_date"] and d["due_date"] < today else "Due"
        table.add(d["due_date"], d["contact"], d["number"], state, d["currency"], d["amount_due_base"])
    table.add(None, "Total", "", "", "", sum((d["amount_due_base"] for d in docs), ZERO), total=True)
    return result


# --- /compare --------------------------------------------------------------------------------------------------

HIGH = Decimal("1.5")  # flag a month at 150% or more of the usual amount...
LOW = Decimal("0.5")   # ...or at 50% or less
DEFAULT_MIN_MOVE = Decimal("100")  # ...but only when it moves by at least this much
RECURRING_SHARE = Decimal("0.75")  # billed in 75% of months or more: a month with nothing is "Missing"
FLAG_ORDER = {"High": 0, "New": 1, "Missing": 2, "Low": 3, "": 4}


def _text(value, ctx):
    return value.strip()


def _amount(value, ctx):
    try:
        amount = Decimal(value.replace(",", "").replace("$", ""))
    except ArithmeticError:
        amount = None
    if amount is None or amount < 0:
        raise CommandError(f'"{value}" isn\'t an amount, e.g. --min 500.')
    return amount


def _by(value, ctx):
    value = value.strip().lower()
    if value in ("account", "accounts"):
        return "account"
    if value in ("supplier", "customer", "contact", "suppliers", "customers", "contacts"):
        return "contact"
    raise CommandError("--by takes account or supplier/customer, e.g. --by account.")


def _month_periods(start, end):
    """Calendar months from ``start`` to ``end``; the first and last may be part months."""
    months, cursor = [], date(start.year, start.month, 1)
    while cursor <= end:
        whole = month_period(cursor.year, cursor.month)
        part_start, part_end = max(whole.start, start), min(whole.end, end)
        label = f"{calendar.month_abbr[cursor.month]} {cursor.year}"
        if (part_start, part_end) != (whole.start, whole.end):
            label += f" ({part_start.day}–{part_end.day})"
        months.append(Period(part_start, part_end, label))
        cursor = add_months(cursor, 1)
    return months


def _is_whole(month):
    return month.start.day == 1 and (month.end + timedelta(days=1)).day == 1


def _flag(amount, usual, *, whole, recurring, earlier_activity, min_move):
    """High, Low, Missing or New against the usual (average) amount for the months that had any."""
    if not earlier_activity:
        return "New" if amount else ""
    if amount >= usual * HIGH and amount - usual >= min_move:
        return "High"
    if not whole:
        return ""  # a month still in progress isn't low or missing yet
    if amount == 0:
        return "Missing" if recurring and usual >= min_move else ""
    if amount <= usual * LOW and usual - amount >= min_move:
        return "Low"
    return ""


def _usual(amounts):
    """Average of the months that had an amount (so quarterly bills compare with quarterly bills)."""
    active = [a for a in amounts if a]
    return (sum(active, ZERO) / len(active)).quantize(Decimal("0.01")) if active else ZERO


@command(
    "compare",
    summary="Compare bills, invoices or a P&L measure month by month to spot unusual, new or missing amounts.",
    usage="/compare --bill|--invoice [<number>] [--supplier|--customer <name or code>] [--account <code or name>] "
          "[--by account] [--<period>] [--detail] [--min <amount>]   or   /compare --revenue|--cos|--gp|--expenses"
          "|--profit [--<period>]",
    params=[
        ("--bill  --invoice", "On their own: scan every supplier (or customer) for last month against the 12 "
                              "months before it"),
        ("--bill <number or supplier>", "A bill number: that bill against the supplier's 12 months to it. "
                                        "Otherwise a supplier name (or part of it) or code, month by month"),
        ("--supplier <name or code>", "One supplier month by month (name can be part of it); --customer for sales"),
        ("--account <code or name>", "Only lines coded to matching accounts, e.g. --account Consulting"),
        ("--by account", "Scan by account instead of by supplier or customer"),
        ("--<period>", "Scan: the month to check (default last month). Month by month: the range (default the "
                       "last 12 months plus this month to date)"),
        ("--detail", "Also list the documents behind each month"),
        ("--min <amount>", "Ignore movements smaller than this (default 100)"),
        ("--revenue --cos --gp --expenses --profit", "Compare P&L measures month by month instead"),
    ],
    examples=["/compare --bill", "/compare --bill --by account", "/compare --bill Origin Energy",
              "/compare --bill --account Consulting --detail", "/compare --bill OE-0912",
              "/compare --invoice --customer Smith", "/compare --expenses --gp >> trend.xlsx"],
)
def compare(ctx, cmd):
    params = read_params(
        cmd, ctx, period=True,
        flags={"bill", "bills", "invoice", "invoices", "detail"} | set(MEASURES) | set(MEASURE_ALIASES),
        keyed={"bill": _text, "invoice": _text, "supplier": _text, "customer": _text, "account": _text,
               "by": _by, "min": _amount},
    )
    flags, values = params.flags, params.values
    measures = _measure_flags(params)
    payable = bool({"bill", "bills"} & flags or {"bill", "supplier"} & set(values))
    receivable = bool({"invoice", "invoices"} & flags or {"invoice", "customer"} & set(values))
    if payable and receivable:
        raise CommandError("Compare bills or invoices, not both: --bill/--supplier or --invoice/--customer.")
    if measures and (payable or receivable or "account" in values):
        raise CommandError("Compare documents or P&L measures in one command, not both.")
    if measures:
        return _compare_measures(ctx, params, measures)
    if not (payable or receivable):
        raise CommandError("Say what to compare: --bill, --invoice or a measure such as --expenses. "
                           "Type /help compare for examples.")
    ledger = "payable" if payable else "receivable"
    filtered = {"bill", "invoice", "supplier", "customer", "account"} & set(values)
    if filtered and values.get("by"):
        raise CommandError("--by is for scanning all suppliers or customers; leave out --supplier/--account to scan.")
    if filtered:
        return _compare_trend(ctx, params, ledger)
    return _compare_scan(ctx, params, ledger)


def _doc_words(ledger):
    return ("bill", "bills", "Supplier") if ledger == "payable" else ("invoice", "invoices", "Customer")


def _account_codes(ctx, text):
    """Account codes matching a code exactly or a name partly; (codes, labels)."""
    text = text.lower()
    found = {}
    for acc in ctx.source.accounts().values():
        if acc["code"] and (acc["code"].lower() == text or text in acc["name"].lower()):
            found[acc["code"]] = f"{acc['code']} {acc['name']}"
    if not found:
        raise CommandError(f'No account code or name matches "{text}". Try /tb to see the accounts.')
    return set(found), sorted(found.values())


def _credit_note_warning(ctx, result):
    if getattr(ctx.source, "credit_notes_unavailable", False):
        result.add(Note("Credit notes aren't included: Xero refused access to them. Reconnect with /connect.",
                        "warning"))


def _doc_note(ledger, min_move, partial):
    single, plural, _ = _doc_words(ledger)
    text = (f"Amounts exclude GST, in base currency at each document's rate; credit notes reduce them. Draft and "
            f"awaiting-approval {plural} aren't included, as in the P&L. \"Usual\" is the average of the months with "
            f"any {plural}. Flags: High ≥150% of usual, Low ≤50%, Missing = nothing in a month for a regular "
            f"{single}, New = none before; movements under {_m(min_move)} aren't flagged.")
    if partial:
        text += " A month still in progress isn't flagged as low or missing."
    return Note(text)


def _compare_scan(ctx, params, ledger):
    """Every supplier (or customer, or account) in one month against the 12 months before it."""
    single, plural, contact_word = _doc_words(ledger)
    min_move = params.values.get("min", DEFAULT_MIN_MOVE)
    by_account = params.values.get("by") == "account"
    today = ctx.today
    if params.period:
        checked = params.period.capped(today)
        if (checked.start.year, checked.start.month) != (checked.end.year, checked.end.month):
            raise CommandError("A scan checks one month, e.g. --September 2026. To see a range month by month, "
                               "name a supplier, customer or account.")
        checked = _month_periods(checked.start, checked.end)[0]
    else:
        checked = _month_periods(*_last_whole_month(today))[0]
    history = _month_periods(add_months(date(checked.start.year, checked.start.month, 1), -12),
                             date(checked.start.year, checked.start.month, 1) - timedelta(days=1))
    docs = ctx.source.documents(ledger, history[0].start, checked.end)
    names = {a["code"]: f"{a['code']} {a['name']}" for a in ctx.source.accounts().values() if a["code"]}

    amounts = {}  # key -> list of 13 amounts (12 history months, then the checked month)
    months = history + [checked]
    for doc in docs:
        index = next((i for i, m in enumerate(months) if doc["date"] and m.start <= doc["date"] <= m.end), None)
        if index is None:
            continue
        if by_account:
            for line in doc["lines"]:
                key = names.get(line["account"], line["account"] or "(no account)")
                amounts.setdefault(key, [ZERO] * 13)[index] += line["amount"]
        else:
            amounts.setdefault(doc["contact"] or "(no contact)", [ZERO] * 13)[index] += doc["amount"]

    whole = _is_whole(checked)
    rows = []
    for key, series in amounts.items():
        past, amount = series[:12], series[12]
        usual = _usual(past)
        billed = sum(1 for a in past if a)
        flag = _flag(amount, usual, whole=whole, recurring=billed >= 12 * RECURRING_SHARE,
                     earlier_activity=billed > 0, min_move=min_move)
        if not amount and not flag:
            continue  # nothing this month and nothing expected
        diff = amount - usual
        rows.append((key, amount, usual, diff, diff / usual if usual else None, f"{billed}/12", flag))
    rows.sort(key=lambda r: (FLAG_ORDER[r[6]], -abs(r[3]), r[0]))

    first_column = "Account" if by_account else contact_word
    result = Result(f"{plural.title()} in {checked.label} against the previous 12 months",
                    subtitle=ctx.settings["name"])
    table = result.add(Table([
        Column(first_column), Column(checked.label, MONEY), Column("Usual month", MONEY), Column("Difference", MONEY),
        Column("%", PCT), Column("Months with " + plural, TEXT), Column("Flag"),
    ]))
    for row in rows:
        table.add(*row)
    month_total = sum((r[1] for r in rows), ZERO)
    monthly = [sum((s[i] for s in amounts.values()), ZERO) for i in range(12)]
    average = (sum(monthly, ZERO) / 12).quantize(Decimal("0.01"))
    table.add("Total", month_total, average, month_total - average, (month_total - average) / average if average else None,
              "", "", total=True)
    if not rows:
        result.add(Note(f"No {plural} in {checked.label}, and none expected."))
    flagged = [r for r in rows if r[6]]
    if flagged:
        result.add(Note(f"{len(flagged)} flagged. See one month by month with /compare --{single} "
                        f"--{'account' if by_account else contact_word.lower()} <name>.", "warning"))
    _credit_note_warning(ctx, result)
    result.add(Note("The total's usual month is the average of all 12 months. " +
                    _doc_note(ledger, min_move, not whole).text))
    return result


def _last_whole_month(today):
    first = date(today.year, today.month, 1)
    return add_months(first, -1), first - timedelta(days=1)


def _compare_trend(ctx, params, ledger):
    """Month by month for one supplier/customer, document's contact or account."""
    single, plural, contact_word = _doc_words(ledger)
    values, today = params.values, ctx.today
    min_move = values.get("min", DEFAULT_MIN_MOVE)
    number = values.get("bill") or values.get("invoice")
    contact_text = (values.get("supplier") or values.get("customer") or "").lower()
    anchor = None

    if number and contact_text:
        raise CommandError(f"Give a {single} number or a {contact_word.lower()}, not both.")
    if number:
        doc = ctx.source.document(number)
        if doc is None:  # not a document number: read it as a supplier or customer name or code
            contact_text = number.lower()
        else:
            ledger = "payable" if doc["type"] == "Bill" else "receivable"
            single, plural, contact_word = _doc_words(ledger)
            contact_text, anchor = doc["contact"].lower(), doc
    if params.period:
        span = params.period.capped(today)
        start, end = span.start, span.end
    elif anchor:
        end = min(month_period(anchor["date"].year, anchor["date"].month).end, today)
        start = add_months(date(end.year, end.month, 1), -11)
    else:
        start, end = add_months(date(today.year, today.month, 1), -12), today
    months = _month_periods(start, end)
    if len(months) > 24:
        raise CommandError("Compare up to 24 months at a time.")

    codes, account_labels = _account_codes(ctx, values["account"]) if "account" in values else (None, [])
    docs = []
    matched_contacts = set()
    for doc in ctx.source.documents(ledger, months[0].start, months[-1].end):
        if contact_text:
            name = doc["contact"].lower()
            if not (name == contact_text or contact_text in name or contact_text in doc["contact_codes"]):
                continue
            matched_contacts.add(doc["contact"])
        amount = doc["amount"] if codes is None else sum((li["amount"] for li in doc["lines"] if li["account"] in codes), ZERO)
        if amount or codes is None:
            docs.append((doc, amount))
    if contact_text and not matched_contacts:
        who = contact_word.lower()
        prefix = f'No {single} numbered "{number}", and no' if number else "No"
        raise CommandError(f'{prefix} {plural} from a {who} matching "{contact_text}" between '
                           f"{fmt(months[0].start)} and {fmt(months[-1].end)}. Check the name with /find {contact_text}.")

    by_month = [ZERO] * len(months)
    numbers = [[] for _ in months]
    for doc, amount in sorted(docs, key=lambda d: (d[0]["date"] or today, d[0]["number"])):
        for i, m in enumerate(months):
            if doc["date"] and m.start <= doc["date"] <= m.end:
                by_month[i] += amount
                label = doc["number"] or "(no number)"
                if anchor and doc["number"] == anchor["number"]:
                    label += " ◀"
                numbers[i].append(label)
    subject = [", ".join(sorted(matched_contacts))] if matched_contacts else []
    subject += account_labels
    title = f"{plural.title()} month by month: {'; '.join(subject)}"
    result = Result(title, subtitle=ctx.settings["name"])
    _trend_table(result, months, by_month, min_move, documents=(plural.title(), numbers))
    if len(matched_contacts) > 1:
        result.add(Note(f"Matched {len(matched_contacts)} {contact_word.lower()}s: {', '.join(sorted(matched_contacts))}. "
                        "Give more of the name to narrow it.", "warning"))
    if "detail" in params.flags:
        detail = result.add(Table([Column("Date", DATE), Column("Number"), Column("Type"), Column(contact_word),
                                   Column("Accounts"), Column("Amount", MONEY)], title="Documents"))
        for doc, amount in sorted(docs, key=lambda d: (d[0]["date"] or today, d[0]["number"])):
            accounts = sorted({li["account"] for li in doc["lines"] if li["account"] and (codes is None or li["account"] in codes)})
            detail.add(doc["date"], doc["number"], doc["type"], doc["contact"], ", ".join(accounts), amount)
    if any(numbers):
        example = next(n for month in reversed(numbers) for n in month if n != "(no number)")
        result.add(Note(f"See any {single} in full with /inv <number>, e.g. /inv {example.rstrip(' ◀')}."
                        + ("" if "detail" in params.flags else " Add --detail to list dates and accounts.")))
    _credit_note_warning(ctx, result)
    result.add(_doc_note(ledger, min_move, not _is_whole(months[-1])))
    return result


def _trend_table(result, months, amounts, min_move, *, title="", documents=None):
    """Month-by-month table. ``documents`` is (column title, list of document numbers per month) to show which
    documents make up each month."""
    whole = [_is_whole(m) for m in months]
    usual = _usual([a for a, w in zip(amounts, whole) if w])
    active_whole = [a for a, w in zip(amounts, whole) if w]
    recurring = bool(active_whole) and sum(1 for a in active_whole if a) >= len(active_whole) * RECURRING_SHARE
    columns = [Column("Month"), Column("Amount", MONEY), Column("Change on prior month", MONEY), Column("%", PCT),
               Column("Against usual", MONEY), Column("%", PCT), Column("Flag")]
    if documents:
        columns.append(Column(documents[0]))
    table = result.add(Table(columns, title=title))
    for i, (month, amount) in enumerate(zip(months, amounts)):
        prior = amounts[i - 1] if i else None
        change = amount - prior if prior is not None else None
        earlier = len([a for a in amounts[:i] if a]) > 0 or i < 3
        flag = _flag(amount, usual, whole=whole[i], recurring=recurring, earlier_activity=earlier, min_move=min_move)
        cells = [month.label, amount, change, change / prior if prior else None, amount - usual,
                 (amount - usual) / usual if usual else None, flag]
        if documents:
            month_numbers = documents[1][i]
            cells.append(", ".join(month_numbers[:6]) + (f" +{len(month_numbers) - 6} more" if len(month_numbers) > 6 else ""))
        table.add(*cells)
    total = sum(amounts, ZERO)
    extra = [""] if documents else []
    table.add("Total", total, None, None, None, None, "", *extra, total=True)
    table.add("Usual month", usual, None, None, None, None, "", *extra, total=True)
    return table


def _compare_measures(ctx, params, measures):
    today = ctx.today
    span = params.period.capped(today) if params.period else Period(
        add_months(date(today.year, today.month, 1), -12), today, "")
    months = _month_periods(span.start, span.end)
    if len(months) > 24:
        raise CommandError("Compare up to 24 months at a time.")
    min_move = params.values.get("min", DEFAULT_MIN_MOVE)
    if set(params.values) - {"min"}:
        raise CommandError("--supplier, --customer, --account and --by work with --bill or --invoice.")
    pls = ctx.source.profit_and_loss_by_month(months)
    result = Result(f"Month by month: {fmt(months[0].start)} to {fmt(months[-1].end)}", subtitle=ctx.settings["name"])
    for key in measures:
        label, measure, _ = MEASURES[key]
        _trend_table(result, months, [pl.measure(measure) for pl in pls], min_move, title=label)
    note = ("\"Usual\" is the average of the whole months shown. Flags: High ≥150% of usual, Low ≤50%; movements "
            f"under {_m(min_move)} aren't flagged.")
    if not _is_whole(months[-1]):
        note += f" {months[-1].label} is to date, so it isn't flagged as low."
    result.add(Note(note))
    return result


# --- Balance sheet reconciliation ---------------------------------------------------------------------------


@command(
    "reconcile", takes_input=True,
    summary="Month-end balance sheet reconciliation: each account's Xero balance against its support, with "
            "reconciling items, schedules and preparer/reviewer sign-off.",
    usage="/reconcile [<account>] [--<period>] [<< schedule.csv] | /reconcile --bank | /reconcile --template",
    params=[
        ("<account>", "Open one account's working paper by code or name, e.g. 090"),
        ("--<period>", "The month to reconcile, e.g. Sep 2026 (its last day is the period end); default last month"),
        ("<< schedule.csv", "The schedule supporting the account (or, with an Account column, several accounts)"),
        ("--template", "Download the schedule template and see how to fill it in"),
        ("--bank", "Bank accounts: Xero transactions not yet reconciled, today"),
    ],
    examples=["/reconcile", "/reconcile --Sep 2026", "/reconcile 090 --Sep 2026", "/reconcile --template",
              "/reconcile 1400 --Sep 2026 << prepayments.csv", "/reconcile --Sep 2026 << schedules.csv",
              "/reconcile --bank", "/reconcile --Sep 2026 >> reconciliations.xlsx"],
    aliases=("recon", "rec"),
)
def reconcile(ctx, cmd):
    from .reconcile import start

    return start(ctx, cmd)


@command(
    "accrue",
    summary="Month-end accrual review: proposed accruals by source with their evidence; accept, adjust or reject "
            "each, then build the journal file.",
    usage="/accrue [--<period>] [--read | --refresh]",
    params=[
        ("--<period>", "The month, e.g. Sep 2026; default last month"),
        ("--read", "Read the PDFs of bills that came after month end (in the background)"),
        ("--refresh", "Work the proposals out again; decided lines are kept"),
    ],
    examples=["/accrue", "/accrue --Sep 2026", "/accrue --read --Sep 2026", "/accrue --refresh"],
)
def accrue(ctx, cmd):
    from .accrue import start

    return start(ctx, cmd)


# --- Lookups ---------------------------------------------------------------------------------------------------


@command(
    "inv",
    summary="Show one invoice, bill or credit note by its number.",
    usage="/inv <number>",
    examples=["/inv INV-0042", "/inv CN-0007"],
    aliases=("invoice", "bill"),
)
def inv(ctx, cmd):
    read_params(cmd, ctx)
    if not cmd.text:
        raise CommandError("Give the invoice, bill or credit note number, e.g. /inv INV-0042.")
    doc = ctx.source.document(cmd.text)
    if doc is None:
        raise CommandError(f"No invoice, bill or credit note numbered {cmd.text}.")
    result = Result(f"{doc['type']} {doc['number']}", subtitle=f"{doc['contact']} · {doc['status']}")
    total = Figure(f"Total ({doc['currency']})", doc["total"], detail=f"tax {_m(doc['tax'])}")
    if doc.get("is_credit"):
        figures = [Figure("Date", doc["date"], DATE), total, Figure("Credit used", doc["paid"],
                   detail="allocated or refunded"), Figure("Credit remaining", doc["due"])]
    else:
        figures = [Figure("Date", doc["date"], DATE), Figure("Due", doc["due_date"], DATE), total,
                   Figure("Paid", doc["paid"]), Figure("Amount due", doc["due"])]
    result.add(Figures(figures))
    table = result.add(Table([Column("Description"), Column("Account"), Column("Qty", MONEY),
                              Column("Unit price", MONEY), Column("Amount", MONEY)]))
    for li in doc["lines"]:
        table.add(li["description"], li["account"], li["quantity"], li["unit"], li["amount"])
    table.add("Subtotal", "", None, None, doc["subtotal"], total=True)
    if doc["reference"]:
        result.add(Note(f"Reference: {doc['reference']}"))
    return result


@command(
    "find",
    summary="Search contacts, and invoices and bills by number or reference.",
    usage="/find <text>",
    examples=["/find acme", "/find INV-004"],
    aliases=("search",),
)
def find(ctx, cmd):
    read_params(cmd, ctx)
    if len(cmd.text) < 2:
        raise CommandError("Type at least two characters to search for, e.g. /find acme.")
    contacts, docs = ctx.source.find(cmd.text)
    result = Result(f'Results for "{cmd.text}"', subtitle=ctx.settings["name"])
    if contacts:
        table = result.add(Table([Column("Contact"), Column("Email"), Column("Customer"), Column("Supplier")],
                                 title="Contacts"))
        for c in contacts:
            table.add(c["name"], c["email"], "Yes" if c["customer"] else "", "Yes" if c["supplier"] else "")
    if docs:
        table = result.add(Table([Column("Number"), Column("Type"), Column("Contact"), Column("Date", DATE),
                                  Column("Status"), Column("Total", MONEY), Column("Due", MONEY)],
                                 title="Invoices and bills"))
        for d in docs:
            table.add(d["number"], d["type"], d["contact"], d["date"], d["status"], d["total"], d["due"])
    if not contacts and not docs:
        result.add(Note("Nothing found."))
    return result


# --- Organisations and connection ------------------------------------------------------------------------------


XERO_ACTIVE_CATEGORIES, XERO_TOTAL_CATEGORIES = 2, 4


@command(
    "dim", aliases=("dimensions", "dims"), needs_source=False,
    summary="What you can tag by: Xero tracking categories (2 active, 4 in all) and iLA's own dimensions (no limit).",
    usage="/dim [<name>] [--archived]",
    params=[("<name>", "Show one tracking category or dimension with its options or values."),
            ("--archived", "Include archived categories, options, dimensions and values.")],
    examples=["/dim", "/dim event", "/dim department --archived"],
)
def dim(ctx, cmd):
    params = read_params(cmd, ctx, flags=("archived",))
    archived = "archived" in params.flags
    if ctx.organisation is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    try:
        categories, xero_problem = ctx.source.tracking_category_list(), ""
    except XeroAPIError as exc:
        categories, xero_problem = [], str(exc)
    dimensions = _dimensions(ctx.organisation)
    if cmd.text:
        return _dim_detail(cmd.text, categories, dimensions, archived)

    result = Result("Dimensions", subtitle="Xero tracking categories and iLA dimensions you can tag by.")
    if xero_problem:
        result.add(Note(f"Xero tracking categories couldn't be read: {xero_problem}", "warning"))
    else:
        active = [c for c in categories if c["status"] == "Active"]
        result.add(Figures([
            Figure("Xero categories in use", f"{len(active)} of {XERO_ACTIVE_CATEGORIES} active", TEXT,
                   detail=f"{len(categories)} of {XERO_TOTAL_CATEGORIES} in all, archived included"),
            Figure("iLA dimensions", str(len([d for d in dimensions if d.is_active])), TEXT,
                   detail="No limit; kept in iLA, never written to Xero"),
        ]))
        table = result.add(Table([Column("Tracking category"), Column("Status"), Column("Active options", NUMBER),
                                  Column("Archived options", NUMBER), Column("iLA dimension")],
                                 title="Xero tracking categories"))
        linked = {d.tracking_category.name: d.name for d in dimensions if d.tracking_category_id}
        for c in categories:
            if c["status"] != "Active" and not archived:
                continue
            table.add(c["name"], c["status"], sum(o["status"] == "Active" for o in c["options"]),
                      sum(o["status"] != "Active" for o in c["options"]), linked.get(c["name"], ""))
        if not table.rows:
            result.add(Note("No tracking categories in Xero yet."))
    table = result.add(Table([Column("Dimension"), Column("Code"), Column("Values", NUMBER), Column("From Xero"),
                              Column("Extra fields"), Column("Tagged items", NUMBER), Column("Status")],
                             title="iLA dimensions"))
    for d in dimensions:
        if not d.is_active and not archived:
            continue
        table.add(d.name, d.code, d.value_count, d.tracking_category.name if d.tracking_category_id else "",
                  ", ".join(f.get("label") or f["key"] for f in d.attribute_fields), d.tag_count,
                  "Active" if d.is_active else "Archived")
    if not table.rows:
        result.add(Note("No iLA dimensions yet. They are set up per organisation (admin → Dimensions) and have no "
                        "limit, unlike Xero's tracking categories."))
    result.add(Note("Type /dim <name> to see a category's options or a dimension's values. Xero allows 2 active and "
                    "4 tracking categories in all; iLA dimensions add as many more as you need without touching "
                    "Xero."))
    return result


def _dimensions(organisation):
    return list(Dimension.objects.filter(organisation=organisation).select_related("tracking_category")
                .annotate(value_count=Count("values", filter=Q(values__is_active=True), distinct=True),
                          tag_count=Count("assignments", distinct=True))
                .order_by("name"))


def _dim_detail(text, categories, dimensions, archived):
    wanted = text.strip().lower()
    cats = [c for c in categories if wanted in c["name"].lower()]
    dims = [d for d in dimensions if wanted in d.name.lower() or wanted == d.code.lower()]
    if not cats and not dims:
        raise CommandError(f'No tracking category or dimension matches "{text}". Type /dim to see them all.')
    result = Result(f"Dimensions: {text}")
    for c in cats:
        table = result.add(Table([Column("Option"), Column("Status")],
                                 title=f"Xero tracking category {c['name']} ({c['status'].lower()})"))
        for o in sorted(c["options"], key=lambda o: o["name"].lower()):
            if o["status"] == "Active" or archived:
                table.add(o["name"], o["status"])
        hidden = sum(o["status"] != "Active" for o in c["options"]) if not archived else 0
        if hidden:
            result.add(Note(f"{hidden} archived option(s) hidden; add --archived to see them."))
    for d in dims:
        fields = [(f["key"], f.get("label") or f["key"]) for f in d.attribute_fields]
        table = result.add(Table([Column("Code"), Column("Value"), Column("Xero option")]
                                 + [Column(label) for _, label in fields] + [Column("Status")],
                                 title=f"iLA dimension {d.name}" + (f" (from Xero {d.tracking_category.name})"
                                                                    if d.tracking_category_id else "")))
        for v in d.values.select_related("tracking_option").order_by("name"):
            if v.is_active or archived:
                table.add(v.code, v.name, v.tracking_option.name if v.tracking_option_id else "",
                          *[_attr(v.attributes.get(key)) for key, _ in fields], "Active" if v.is_active else "Archived")
        if not table.rows:
            result.add(Note(f"{d.name} has no values yet."))
    return result


def _attr(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


@command(
    "tag",
    summary="Tag invoices, bills, credit notes, bank transactions or journals to a value of an iLA dimension, "
            "e.g. events. Preview first; nothing is tagged until you approve.",
    usage="/tag --dim <dimension> --invoices|--bills|--credits|--bank|--journals|--all --<period> [--desc <text>] "
          "[--contact <name>] [--account <code or name>] [--amount <range>] [--xero <category> [<option>]] "
          "[--attachment] [--retag] [--to <value>] [--window <days>]",
    params=[("--dim <dimension>", "The iLA dimension to tag to (not needed if you have only one)."),
            ("--invoices --bills --credits --bank --journals", "What to tag; any of them. --all for every type. "
                                                               "Journals are manual journals."),
            ("--<period>", "Mar 2026, last month, Jul 2026 to Sep 2026. Both end dates are included."),
            ("--desc <text>", "Description, reference or number contains it; ABBA or Bjorn for either."),
            ("--contact <name>", "The contact's name contains it."),
            ("--account <code or name>", "Only lines on these accounts (default: P&L lines)."),
            ("--amount <range>", ">1000, <500, 500 to 5000 or 1200 (the P&L amount)."),
            ("--xero <category> [<option>]", "Only lines Xero already tracks in that category (and option)."),
            ("--attachment", "Only transactions with a file attached in Xero."),
            ("--retag", "Include transactions already tagged in the dimension."),
            ("--to <value>", "Tag every match to this value (code or name) instead of proposals."),
            ("--window <days>", "How far either side of a value's dates still counts as fitting (default 3).")],
    examples=["/tag --dim event --bills --invoices --Mar 2026",
              "/tag --dim event --all --Jul 2026 to Sep 2026 --desc ABBA --to 260328ABB",
              "/tag --dim event --bills --last month --contact Ticketek --amount >500",
              "/tag --dim event --journals --FY2026 --xero Event --retag"],
)
def tag(ctx, cmd):
    from .tag_run import start

    return start(ctx, cmd)


@command(
    "splittag", takes_input=True,
    summary="Split the amounts on chosen GL accounts across a dimension's values (e.g. labour across events) by "
            "fixed amounts, percentages or both, from a CSV of named rules or a saved rule.",
    usage="/splittag [<< rules.csv] [--<rule name>|--rule <name>] [--<period>] [--gl <accounts>] "
          "[--invoices --bills --credits --bank --journals|--all] [--contact <name>] [--desc <text>] [--template]",
    params=[("<< rules.csv", "Rules to apply; approving also saves them by name. /splittag --template for the file."),
            ("--<rule name>  or  --rule <name>", "Run a saved rule (several: --rule labour, casuals)."),
            ("--<period>", "Sep 2026, last month, Jul 2026 to Sep 2026; both dates included. Needed unless the file "
                           "has From and To."),
            ("--gl <accounts>", "Change the accounts for this run: 6300-6380, 6355 or 6300-6380; 6400."),
            ("--invoices --bills --credits --bank --journals --all", "Change the transaction types for this run."),
            ("--contact <name>  --desc <text>", "Only lines whose contact or description contains it."),
            ("--template", "Download the CSV template.")],
    examples=["/splittag", "/splittag --template", "/splittag --Sep 2026 << labour.csv",
              "/splittag --labour --Sep 2026", "/splittag --gl 6300-6380 --sep-2026 --labour",
              "/splittag --rule casuals --Oct 2026 --journals"],
)
def splittag(ctx, cmd):
    from .split_tag import start

    return start(ctx, cmd)


@command(
    "createtag", needs_source=False, takes_input=True,
    summary="Set up a dimension to tag by from a CSV of its values: brand new, or replacing a Xero tracking "
            "category so iLA shows names instead of codes.",
    usage="/createTag [<name>] [--new] [<< values.csv]",
    params=[("<name>", "The Xero tracking category or iLA dimension to set up, or the name of a new one."),
            ("--new", "Drop the open /createTag draft and start again."),
            ("<< values.csv", "The values file. With a name, iLA keeps that name and goes straight to the preview.")],
    examples=["/createTag", "/createTag Event", "/createTag Event << events.csv", "/createTag Venue"],
)
def createtag(ctx, cmd):
    params = read_params(cmd, ctx, flags=("new",))
    from .create_tag import start

    return start(ctx, cmd, params)


@command("orgs", summary="List your organisations; * marks the one in use.", examples=["/orgs"], needs_source=False)
def orgs(ctx, cmd):
    read_params(cmd, ctx)
    result = Result("Organisations")
    table = result.add(Table([Column(""), Column("Organisation"), Column("Connected to")]))
    for org in ctx.user.organisations.order_by("name"):
        connection = getattr(org, "xero_connection", None)
        state = "Xero" if connection and connection.is_active else "Xero (disconnected)" if connection else "Nothing"
        table.add("*" if ctx.organisation and org.pk == ctx.organisation.pk else "", org.name, state)
    if not table.rows:
        result.add(Note("No organisations yet. Connect one with /connect."))
    return result


@command("use", summary="Switch to another organisation.", usage="/use <name>", examples=["/use Demo Company"],
         needs_source=False)
def use(ctx, cmd):
    read_params(cmd, ctx)
    if not cmd.text:
        raise CommandError("Give the organisation name, e.g. /use Demo Company. /orgs lists them.")
    matches = [o for o in ctx.user.organisations.all() if cmd.text.lower() in o.name.lower()]
    exact = [o for o in matches if o.name.lower() == cmd.text.lower()]
    if len(exact) == 1 or len(matches) == 1:
        org = (exact or matches)[0]
        ctx.session["organisation_id"] = org.pk
        ctx.organisation = org
        return Result(f"Now using {org.name}")
    if not matches:
        raise CommandError(f'No organisation matches "{cmd.text}". /orgs lists them.')
    raise CommandError(f'"{cmd.text}" matches {", ".join(o.name for o in matches)}. Be more specific.')


@command("connect", summary="Connect or reconnect Xero (grants any new permissions).", examples=["/connect"],
         needs_source=False)
def connect(ctx, cmd):
    read_params(cmd, ctx)
    result = Result("Connect to Xero")
    result.add(Note(f"Open {reverse('xero:connect')} to sign in to Xero and choose the organisations to connect. "
                    "Reconnecting also grants any permissions added since you last connected."))
    result.link = reverse("xero:connect")
    return result


# --- Help ------------------------------------------------------------------------------------------------------


@command("clean", summary="Clear the results on screen. Your command history (up arrow) is kept.", usage="/clean",
         examples=["/clean"], aliases=("clear", "cls"), needs_source=False)
def clean(ctx, cmd):
    # The console page clears itself without calling the server; this answers any other client.
    read_params(cmd, ctx)
    return Result("Nothing to clear", subtitle="/clean clears the results shown in the console page.")


@command("cancel", summary="Drop your open /splittag or /tag preview, /createTag or /_createInvoice draft, or "
                           "close a /reconcile working paper or /accrue review. Nothing is saved.",
         usage="/cancel", examples=["/cancel"], needs_source=False)
def cancel(ctx, cmd):
    read_params(cmd, ctx)
    from . import create_invoice
    from .dialogues import open_dialogue

    module, _ = open_dialogue(ctx)
    return (module or create_invoice).cancel(ctx)


@command("help", summary="List commands, or show how to use one: /help status.", usage="/help [command]",
         examples=["/help", "/help status"], aliases=("?",), needs_source=False)
def help_(ctx, cmd):
    read_params(cmd, ctx)
    built = active_commands(ctx.organisation)
    if cmd.text.lstrip("/").startswith("_"):
        custom = next((c for c in built if c.name.lower() == cmd.text.lstrip("/").lower()), None)
        if custom is None:
            raise CommandError(f"There's no /{cmd.text.lstrip('/')} command. Type /help to see them all.")
        result = Result(f"/{custom.name}", subtitle=custom.summary)
        if custom.template == "createinvoice":
            result.add(Note(f"Usage: /{custom.name} [customer] [--new]. A guided conversation: customer, event or "
                            "job, billing information (files with << or typed details), a draft on screen, changes "
                            "in plain English, then approve to build the import file. Plain answers (no /) go to "
                            "your open draft; /cancel drops it."))
            return result
        result.add(Note(f"Built in the builder ({custom.get_template_display()}). It runs:"))
        table = result.add(Table([Column("Step", NUMBER), Column("Command")]))
        for number, step in enumerate(custom.steps, 1):
            table.add(number, step)
        joined = "\n".join(custom.steps)
        if "{text}" in joined or "{params}" in joined:
            result.add(Note("{text} is replaced by what you type after the name, {params} by your -- parameters."))
        return result
    if cmd.text:
        spec = lookup(cmd.text.lstrip("/").lower())
        if spec is None:
            raise CommandError(f"There's no /{cmd.text.lstrip('/')} command. Type /help to see them all.")
        result = Result(f"/{spec.name}", subtitle=spec.summary)
        result.add(Note(f"Usage: {spec.usage}"))
        if spec.params:
            table = result.add(Table([Column("Parameter"), Column("Meaning")]))
            for syntax, meaning in spec.params:
                table.add(syntax, meaning)
        if spec.examples:
            result.add(Note("Examples: " + "   ".join(spec.examples)))
        if spec.aliases:
            result.add(Note("Also: " + ", ".join(f"/{a}" for a in spec.aliases)))
        return result
    result = Result("Commands", subtitle="Parameters follow -- in any order. << input file, >> output file "
                                         "(.xlsx or .csv).")
    table = result.add(Table([Column("Command"), Column("What it does")]))
    for spec in COMMANDS.values():
        table.add(spec.usage, spec.summary)
    if built:
        own = result.add(Table([Column("Command"), Column("What it does")],
                               title=f"{ctx.organisation.name}'s own commands"))
        for custom in built:
            own.add(f"/{custom.name}", custom.summary)
    return result
