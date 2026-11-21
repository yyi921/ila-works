"""/pl --vs lm|ly|budget and /pl --landing: the P&L by account against one comparison, optionally with the overlay
journals on top (Xero · To be posted · Draft · After; the variance is then on After).

Variances are plain: actual less the comparison, and that as a % of the comparison. The comparison is always Xero's
own figure (last month's actuals, last year's actuals, the budget), never another month's overlay journals.
"""

import calendar
from decimal import Decimal

from core import overlay
from core.models import Organisation
from xero.source import INCOME_CATEGORIES

from .output import MONEY, PCT, Column, Note, Result, Table
from .periods import Period, add_months
from .registry import CommandError

ZERO = Decimal("0")
VS = {"lm": "lm", "last month": "lm", "ly": "ly", "last year": "ly", "budget": "budget", "bud": "budget"}
SECTIONS = [("revenue", "Revenue"), ("other_income", "Other income"), ("cost_of_sales", "Cost of sales"),
            ("expenses", "Expenses")]


def read_vs(value, ctx=None):
    key = VS.get(" ".join(value.lower().split()))
    if key is None:
        raise CommandError(f'--vs {value}: use lm (last month), ly (the same period last year) or budget.')
    return key


def _last_month(period):
    """The month before a one-month period (the same days of it for a part month)."""
    if (period.start.year, period.start.month) != (period.end.year, period.end.month):
        raise CommandError("--vs lm compares one month with the month before: give one month, e.g. /pl --Oct 2026 "
                           "--vs lm (or use --vs ly or --vs budget).")
    start = add_months(period.start, -1)
    whole = period.end.day == calendar.monthrange(period.end.year, period.end.month)[1]
    last_day = calendar.monthrange(start.year, start.month)[1]
    end = start.replace(day=last_day if whole else min(period.end.day, last_day))
    label = f"{calendar.month_name[start.month]} {start.year}" if whole and period.start.day == 1 else \
        f"{start:%d/%m}–{end:%d/%m/%Y}"
    return Period(start, end, label)


def _overlay(ctx, period, accounts):
    """{account id: [to be posted, draft]} as the P&L shows them (income and costs positive), for the overlay
    journals this person sees; plus how many journals."""
    if not isinstance(ctx.organisation, Organisation):
        return {}, 0
    journals = overlay.in_period(overlay.visible(ctx.organisation, ctx.user), period.start, period.end)
    by_code = {info["code"]: account for account, info in accounts.items() if info.get("code")}
    out = {}
    for code, amounts in overlay.effect(journals, period.start, period.end).items():
        account = by_code.get(code)
        category = accounts.get(account, {}).get("category") if account else None
        if category is None:
            continue  # a balance sheet account
        sign = -1 if category in INCOME_CATEGORIES else 1
        out[account] = [sign * amounts["to_post"], sign * amounts["draft"]]
    return out, len(journals)


def report(ctx, period, vs, landing):
    src = ctx.source
    accounts = src.accounts()
    compare, compare_label, notes = None, "", []
    if vs == "ly":
        current, compare = src.profit_and_loss(period.start, period.end, with_last_year=True)
        compare_label = "Last year"
    else:
        current = src.profit_and_loss(period.start, period.end)
        if vs == "lm":
            last = _last_month(period)
            compare, compare_label = src.profit_and_loss(last.start, last.end), last.label
        elif vs == "budget":
            compare, compare_label = src.budget(period.start, period.end), "Budget"
            if compare is None:
                notes.append(Note("There's no overall budget in Xero for this period (or iLA lacks the budgets "
                                  "permission: reconnect with /connect).", "warning"))
    on_top, count = _overlay(ctx, period, accounts) if landing else ({}, 0)

    columns = [Column("Code"), Column("Account")]
    columns += [Column("Xero", MONEY), Column("To be posted", MONEY), Column("Draft", MONEY),
                Column("After", MONEY)] if landing else [Column("Actual", MONEY)]
    if vs:
        columns += [Column(compare_label, MONEY), Column("Var $", MONEY), Column("Var %", PCT)]
    title = f"Profit and loss: {period.label}" + (f" vs {compare_label.lower()}" if vs else "")
    result = Result(title, subtitle=ctx.settings["name"] + (" · with overlay journals" if landing else ""))
    table = result.add(Table(columns))

    def row(code, label, xero, top, other, *, total=False, heading=False):
        if heading:
            table.add(code, label, *[None] * (len(columns) - 2), total=True)
            return
        after = xero + top[0] + top[1]
        cells = [code, label] + ([xero, top[0] or None, top[1] or None, after] if landing else [xero])
        if vs:
            if other is None:
                cells += [None, None, None]
            else:
                diff = after - other
                cells += [other, diff, (diff / abs(other)) if other else None]
        table.add(*cells, total=total, tone="touch" if landing and not total and (top[0] or top[1]) else "")

    def amounts(ids):
        return (sum((current.by_account.get(a, ZERO) for a in ids), ZERO),
                [sum((on_top.get(a, [ZERO, ZERO])[i] for a in ids), ZERO) for i in (0, 1)],
                sum((compare.by_account.get(a, ZERO) for a in ids), ZERO) if compare else None)

    def name(account):
        if account in accounts:
            return accounts[account]["name"]
        return next((p.names[account] for p in (current, compare) if p and account in p.names), account)

    totals = {}
    for category, heading in SECTIONS:
        ids = {a for pl in (current, compare) if pl for a, c in pl.category_of.items() if c == category}
        ids |= {a for a in on_top if accounts.get(a, {}).get("category") == category}
        totals[category] = amounts(ids)
        if not ids and category == "other_income":
            continue
        row("", heading, ZERO, [ZERO, ZERO], None, heading=True)
        for account in sorted(ids, key=lambda a: (accounts.get(a, {}).get("code", ""), name(a))):
            row(accounts.get(account, {}).get("code", ""), name(account), *amounts({account}))
        row("", f"Total {heading.lower()}", *totals[category], total=True)
        if category == "cost_of_sales":
            row("", "Gross profit", *_net(totals, ("revenue",), ("cost_of_sales",)), total=True)
    other = (-current.unattributed, -compare.unattributed if compare else None)
    if other[0] or other[1]:
        row("", "Other items not linked to an account (e.g. currency gains/losses)", other[0], [ZERO, ZERO],
            other[1])
    net = _net(totals, ("revenue", "other_income"), ("cost_of_sales", "expenses"))
    row("", "Net profit", current.net_profit, net[1], compare.net_profit if compare else None, total=True)

    for note in notes:
        result.add(note)
    if landing:
        result.add(Note(f"After = Xero + {count} overlay journal{'s' if count != 1 else ''} dated or reversing in "
                        "the period (yours, iLA's drafts and everything To be posted)."
                        + (" The variance is on After." if vs else "") + " /pl --xero leaves them out."))
    return result


def _net(totals, plus, minus):
    """(xero, [to be posted, draft], comparison) of some sections less others."""
    def side(index):
        values = [(totals[c][index], 1) for c in plus] + [(totals[c][index], -1) for c in minus]
        if index == 1:
            return [sum((v[i] * s for v, s in values), ZERO) for i in (0, 1)]
        if any(v is None for v, _ in values):
            return None
        return sum((v * s for v, s in values), ZERO)
    return side(0), side(1), side(2)
