"""The core P&L checks (C1–C8) for one month: every P&L line and subtotal against last month, the same month last
year and budget, year to date against budget, and the COGS, labour, EBITDA and net profit ratios. Only movements
past each check's materiality become findings.

Figures come from the source in four calls: the 13 months ending with the month (Xero's monthly comparative
columns, which also give last month, last year, the 12-month pattern and year to date), and the budget for the
month and for the year to date. Amounts are as Xero's P&L shows them: income and costs both positive.
Each finding gets a reading worked out by code: how it moved, against what the account usually does, what is
waiting in /accrue, and any note a person left on the same subject in an earlier month.
"""

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from console.periods import add_months, month_period
from xero.source import ProfitAndLoss

from .models import CHECKS, ReviewFinding

ZERO = Decimal("0")
HUNDRED = Decimal("100")
SECTIONS = [("revenue", "Revenue", True), ("other_income", "Other income", True),
            ("cost_of_sales", "Cost of sales", False), ("expenses", "Expenses", False)]
COMPARED = {"last_month": "last month", "last_year": "last year", "budget": "budget", "ytd_budget": "YTD budget"}
LABOUR_WORDS = r"wage|salar|super|payroll|workcover|work cover|leave|staff|labou?r|bonus"
# ratio key -> (label, higher is favourable)
RATIOS = {"C5": ("cogs", "COGS %", False), "C6": ("labour", "Labour %", False),
          "C7": ("ebitda", "EBITDA %", True), "C8": ("np", "Net profit %", True)}


@dataclass
class Finding:
    key: str  # check + subject, e.g. C3:6345
    subject: str  # e.g. 6345, gp, ratio:labour
    check: str  # C1… or RR-01…
    label: str
    current: Decimal = None
    compare: Decimal = None
    movement: Decimal = None  # favourable positive
    percent: Decimal = None  # movement as % of compare (10 = 10%)
    effect: Decimal = None  # ratio checks: dollar effect of the move
    is_ratio: bool = False
    is_total: bool = False
    reading: str = ""
    compared: str = ""
    category: str = ""
    series: list = field(default_factory=list)  # (month label, amount) oldest first, for why
    table: dict = field(default_factory=dict)  # a review request's result: {"columns": [[label, kind]], "rows": []}
    status: str = "open"
    note: str = ""
    action: str = ""

    def to_json(self):
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data):
        data = dict(data)
        for name in ("current", "compare", "movement", "percent", "effect"):
            if data.get(name) is not None:
                data[name] = Decimal(data[name])
        return cls(**data)


@dataclass
class Figures:
    month: object  # Period
    months: list  # Periods, oldest first: 13 months ending with month
    pls: list  # ProfitAndLoss per month
    budget: object  # ProfitAndLoss or None
    ytd: object
    ytd_budget: object
    accounts: dict
    ytd_start: object = None

    @property
    def current(self):
        return self.pls[-1]

    @property
    def last_month(self):
        return self.pls[-2]

    @property
    def last_year(self):
        return self.pls[0]


def gather(src, month, fy):
    months = [month_period(d.year, d.month) for d in (add_months(month.start, -i) for i in range(12, -1, -1))]
    pls = src.profit_and_loss_by_month(months)
    budget = src.budget(month.start, month.end)
    ytd_start = fy.bounds(month.end)[0]
    ytd = combine([pl for m, pl in zip(months, pls) if m.start >= ytd_start])
    ytd_budget = src.budget(ytd_start, month.end) if budget is not None else None
    return Figures(month, months, pls, budget, ytd, ytd_budget, src.accounts(), ytd_start)


def combine(pls):
    total = ProfitAndLoss()
    for pl in pls:
        for account, amount in pl.by_account.items():
            total.by_account[account] = total.by_account.get(account, ZERO) + amount
        total.names |= pl.names
        total.category_of |= pl.category_of
        total.net_profit += pl.net_profit
    return total


# --- Account groups for the ratios --------------------------------------------------------------------------------

def _codes(text):
    """"6300-6380, 6390" -> a test for an account code."""
    ranges = []
    for part in re.split(r"\s*[,;]\s*", text or ""):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
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


def account_groups(settings, accounts):
    """{group: (account ids, how it was chosen)} for cogs, labour, interest, tax and depreciation."""
    costs = {a: v for a, v in accounts.items() if v["category"] in ("cost_of_sales", "expenses")}

    def named(pattern):
        return {a for a, v in costs.items() if re.search(pattern, v["name"], re.I)}

    defaults = {
        "cogs": ({a for a, v in accounts.items() if v["category"] == "cost_of_sales"}, "every Cost of sales account"),
        "labour": (named(LABOUR_WORDS), "accounts named wages, salaries, super, payroll, leave, Workcover or staff"),
        "interest": (named(r"\binterest\b"), "cost accounts named interest"),
        "tax": (named(r"income tax"), "cost accounts named income tax"),
        "depreciation": ({a for a, v in costs.items() if v["type"] == "DEPRECIATN"}
                         | named(r"depreciat|amortis|amortiz"),
                         "Depreciation accounts and those named depreciation or amortisation"),
    }
    chosen = {"cogs": settings.cogs_accounts, "labour": settings.labour_accounts,
              "interest": settings.interest_accounts, "tax": settings.tax_accounts,
              "depreciation": settings.depreciation_accounts}
    groups = {}
    for name, text in chosen.items():
        if text.strip():
            test = _codes(text)
            groups[name] = ({a for a, v in accounts.items() if v["code"] and test(v["code"])}, f"set in admin: {text}")
        else:
            groups[name] = defaults[name]
    return groups


def ratio_values(pl, groups):
    """(revenue, {ratio key: amount}) for one ProfitAndLoss."""
    revenue = pl.total("revenue")

    def total(name):
        return sum((pl.by_account.get(a, ZERO) for a in groups[name][0]), ZERO)

    ebitda = pl.net_profit + total("interest") + total("tax") + total("depreciation")
    return revenue, {"cogs": total("cogs"), "labour": total("labour"), "ebitda": ebitda, "np": pl.net_profit}


# --- The checks --------------------------------------------------------------------------------------------------

def core_findings(organisation, figures, checks, groups, *, accrued=None):
    """Findings of the enabled core checks, in check order, biggest first within a check. ``checks``: CoreCheck
    objects by code. ``accrued``: {account code: amount accepted in /accrue, not yet in Xero}."""
    notes = earlier_notes(organisation, figures.month.end)
    accrued = accrued or {}
    found = []
    pairs = {"C1": (figures.current, figures.last_month), "C2": (figures.current, figures.last_year),
             "C3": (figures.current, figures.budget), "C4": (figures.ytd, figures.ytd_budget)}
    for code, (name, kind, compared) in CHECKS.items():
        check = checks.get(code)
        if check is None or not check.enabled:
            continue
        if kind == "lines":
            now, other = pairs[code]
            if other is None:
                continue
            rows = _line_findings(code, compared[0], now, other, figures, check)
            rows.sort(key=lambda f: -abs(f.movement))
        else:
            rows = _ratio_findings(code, figures, check, groups)
        for f in rows:
            f.reading = _reading(f, figures, notes, accrued)
        found.extend(rows)
    return found


def _rows(now, other, accounts):
    """(subject, label, category, current, compare, higher is good, is total, account id) for every line and
    subtotal with an amount in either."""
    out = []
    ids_of = {}
    for pl in (now, other):
        for account, category in pl.category_of.items():
            if category:
                ids_of.setdefault(category, set()).add(account)
    for category, title, good_high in SECTIONS:
        ids = ids_of.get(category, set())
        for account in sorted(ids, key=lambda a: accounts.get(a, {}).get("code", "")):
            info = accounts.get(account, {})
            name = info.get("name") or now.names.get(account) or other.names.get(account, "")
            label = f"{info.get('code', '')} {name}"
            out.append((info.get("code") or account, label.strip(), category, now.by_account.get(account, ZERO),
                        other.by_account.get(account, ZERO), good_high, False, account))
        if ids:
            out.append((f"total:{category}", f"Total {title.lower()}", category, now.total(category),
                        other.total(category), good_high, True, None))
        if category == "cost_of_sales":
            out.append(("gp", "Gross profit", "", now.gross_profit, other.gross_profit, True, True, None))
    out.append(("np", "Net profit", "", now.net_profit, other.net_profit, True, True, None))
    return [r for r in out if r[3] or r[4]]


def _line_findings(code, compared, now, other, figures, check):
    found = []
    for subject, label, category, current, compare, good_high, is_total, account in _rows(now, other, figures.accounts):
        diff = current - compare
        movement = diff if good_high else -diff
        percent = (diff / abs(compare) * HUNDRED) if compare else None
        if not check.material(diff, percent):
            continue
        series = [(m.start.strftime("%b %Y"), str(_amount(pl, subject, account))) for m, pl in
                  zip(figures.months, figures.pls)]
        found.append(Finding(f"{code}:{subject}", subject, code, label, current, compare, movement,
                             percent.quantize(Decimal("0.1")) if percent is not None else None, is_total=is_total,
                             compared=compared, category=category, series=series))
    return found


def _amount(pl, subject, account):
    if account:
        return pl.by_account.get(account, ZERO)
    if subject == "gp":
        return pl.gross_profit
    if subject == "np":
        return pl.net_profit
    return pl.total(subject.removeprefix("total:"))


def _ratio_findings(code, figures, check, groups):
    key, label, good_high = RATIOS[code]
    revenue, values = ratio_values(figures.current, groups)
    if not revenue:
        return []
    now = values[key] / revenue
    found = []
    for compared, pl in (("last_month", figures.last_month), ("last_year", figures.last_year),
                         ("budget", figures.budget)):
        if pl is None:
            continue
        other_revenue, other_values = ratio_values(pl, groups)
        if not other_revenue:
            continue
        other = other_values[key] / other_revenue
        points = (now - other) * HUNDRED
        effect = (now - other) * revenue
        percent = (now - other) / abs(other) * HUNDRED if other else None
        if not check.material(effect, percent, points):
            continue
        movement = points if good_high else -points
        series = []
        for m, p in zip(figures.months, figures.pls):
            r, v = ratio_values(p, groups)
            value = str((v[key] / r * HUNDRED).quantize(Decimal("0.1"))) if r else ""
            series.append((m.start.strftime("%b %Y"), value))
        found.append(Finding(f"{code}:{compared}", f"ratio:{key}", code, f"{label} vs {COMPARED[compared]}",
                             now, other, movement.quantize(Decimal("0.01")),
                             percent.quantize(Decimal("0.1")) if percent is not None else None,
                             (effect if good_high else -effect).quantize(Decimal("0.01")), is_ratio=True,
                             compared=compared, series=series))
    return found


# --- Readings ----------------------------------------------------------------------------------------------------

def earlier_notes(organisation, period_end):
    """{subject: (period_end, note)}: the latest note on each subject before this month."""
    notes = {}
    for f in (ReviewFinding.objects.filter(organisation=organisation, period_end__lt=period_end).exclude(note="")
              .order_by("period_end")):
        notes[f.subject] = (f.period_end, f.note)
    return notes


def _money(value):
    return f"{abs(value):,.0f}"


def _reading(f, figures, notes, accrued):
    parts = []
    if f.is_ratio:
        parts.append(_ratio_reading(f, figures))
    else:
        word = "favourable" if f.movement > 0 else "unfavourable"
        if not f.compare:
            parts.append(f"nothing {'budgeted' if 'budget' in f.compared else 'in ' + COMPARED[f.compared]}")
        else:
            parts.append(f"{_money(f.movement)} {word} ({abs(f.percent):.0f}%)")
        earlier = [Decimal(v) for _, v in f.series[:-1] if v != ""] if f.check != "C4" else []
        usual = sum(earlier, ZERO) / len(earlier) if earlier else ZERO
        costs = f.category in ("cost_of_sales", "expenses")
        if costs and not f.current and usual > 0 and sum(1 for v in earlier if v) >= 6:
            parts.insert(0, f"nothing this month yet, usually {_money(usual)}: bill, journal or accrual missing?")
        elif costs and usual > 0 and f.current < usual / 2 and f.check != "C4":
            parts.insert(0, f"less than half the usual month ({_money(usual)}): anything missing?")
        hinted = parts[0].endswith("?") if parts else False
        if f.check in ("C1", "C2"):
            if earlier and not hinted:
                if usual and abs(f.current - usual) <= abs(usual) / 10:
                    parts.append(f"in line with the usual month ({_money(usual)}, 12-month average)")
                elif usual:
                    side = "above" if f.current > usual else "below"
                    parts.append(f"{side} the usual month ({_money(usual)}, 12-month average)")
        if f.check == "C1" and not f.compare and f.series and Decimal(f.series[0][1]):
            parts.append(f"also {_money(Decimal(f.series[0][1]))} a year ago: seasonal?")
        waiting = accrued.get(f.subject)
        if waiting and f.check != "C4":
            parts.append(f"{_money(waiting)} accepted in /accrue, not yet in Xero")
    if f.subject in notes:
        when, text = notes[f.subject]
        parts.append(f'your note ({when:%b %Y}): "{text}"')
    text = "; ".join(p for p in parts if p)
    return text[:1].upper() + text[1:]


def _ratio_reading(f, figures):
    side = "better" if f.movement > 0 else "worse"
    text = f"{abs(f.current - f.compare) * HUNDRED:.1f} points {side} ({f.compare * HUNDRED:.1f}% → " \
           f"{f.current * HUNDRED:.1f}%), worth {_money(f.effect)}"
    return text


def describe_groups(groups, accounts):
    """Lines saying which accounts each ratio uses, for the review's notes."""
    out = []
    for name in ("cogs", "labour"):
        ids, how = groups[name]
        codes = sorted(accounts[a]["code"] for a in ids if a in accounts and accounts[a]["code"])
        out.append(f"{name.upper() if name == 'cogs' else name.capitalize()}: {', '.join(codes) or 'none'} ({how})")
    return out

