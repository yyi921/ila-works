"""The figures behind /review: every P&L account for the month and the year to date, against last month, the same
period last year and budget, each account placed in one of the review's groups (Revenue, Cost of sales, Other
income, Labour, Other operating costs, Depreciation & amortisation, Interest, Income tax).

Figures come from the source in five calls: the month with the same month last year, last month, the year to date
with the same dates last year, and the budget for the month and the year to date. Amounts are as Xero's P&L shows
them: income and costs both positive. Groups come from Review settings; cost accounts in no group are Other
operating costs. Net profit not on any account row (e.g. currency gains and losses) is kept as its own line in
Other operating costs, so the groups add up to Xero's net profit.
"""

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from console.periods import add_months, month_period

ZERO = Decimal("0")
LABOUR_WORDS = r"wage|salar|super|payroll|workcover|work cover|leave|staff|labou?r|bonus"

# Group key -> (label, other words people may type for it). In the order they show.
GROUPS = {
    "rev": ("Revenue", ("revenue", "sales", "income")),
    "cogs": ("Cost of sales", ("cos", "cost of sales", "costofsales", "direct costs")),
    "oth": ("Other income", ("other income", "otherincome")),
    "lab": ("Labour", ("labour", "labor", "wages", "payroll")),
    "opx": ("Other operating costs", ("opex", "overheads", "other costs", "expenses")),
    "da": ("Depreciation & amortisation", ("depreciation", "d&a", "amortisation")),
    "int": ("Interest", ("interest",)),
    "tax": ("Income tax", ("income tax",)),
}
COST_GROUPS = ("cogs", "lab", "da", "int", "tax")  # chosen in settings; the rest of the costs are opx
INCOME = ("rev", "oth")

# Column key -> label. "cur" and "ytd" are actuals; the rest are what they're compared with.
COLUMNS = {"cur": "Actual", "lm": "Last month", "ly": "Last year", "bud": "Budget",
           "ytd": "Actual", "ytd_ly": "Last year", "ytd_bud": "Budget"}
UNATTRIBUTED = "unattributed"


@dataclass
class Line:
    id: str  # Xero account id, or "unattributed"
    code: str  # the reference people type; "" if the account has no code
    name: str
    group: str
    values: dict = field(default_factory=dict)  # column key -> Decimal

    def to_json(self):
        return asdict(self) | {"values": {k: str(v) for k, v in self.values.items()}}

    @classmethod
    def from_json(cls, data):
        return cls(**(data | {"values": {k: Decimal(v) for k, v in data["values"].items()}}))


def gather(src, settings, month, fy):
    """(lines, budget found) for the month: one Line per P&L account with an amount in any column."""
    last = add_months(month.start, -1)
    last_month = month_period(last.year, last.month)
    ytd_start = fy.bounds(month.end)[0]
    cur, ly = src.profit_and_loss(month.start, month.end, with_last_year=True)
    pls = {"cur": cur, "ly": ly, "lm": src.profit_and_loss(last_month.start, last_month.end)}
    pls["ytd"], pls["ytd_ly"] = src.profit_and_loss(ytd_start, month.end, with_last_year=True)
    budget = src.budget(month.start, month.end)
    if budget is not None:
        pls["bud"], pls["ytd_bud"] = budget, src.budget(ytd_start, month.end)
    accounts = src.accounts()
    group_of = classify(settings, accounts)
    lines = {}
    for column, pl in pls.items():
        for account, amount in pl.by_account.items():
            category = pl.category_of.get(account) or accounts.get(account, {}).get("category")
            if not category:
                continue
            if account not in lines:
                info = accounts.get(account, {})
                lines[account] = Line(account, info.get("code") or "", info.get("name") or pl.names.get(account, ""),
                                      group_of.get(account) or _fallback(category))
            lines[account].values[column] = lines[account].values.get(column, ZERO) + amount
        other = pl.unattributed
        if other:
            line = lines.setdefault(UNATTRIBUTED, Line(UNATTRIBUTED, "", "Not on an account row (e.g. currency "
                                                                        "gains and losses)", "opx"))
            line.values[column] = -other  # a cost: a loss lowers net profit
    found = [line for line in lines.values() if any(line.values.values())]
    for line in found:
        for column in pls:
            line.values.setdefault(column, ZERO)
    found.sort(key=lambda line: (list(GROUPS).index(line.group), line.code or "~", line.name))
    return found, budget is not None


def _fallback(category):
    return {"revenue": "rev", "other_income": "oth", "cost_of_sales": "cogs"}.get(category, "opx")


# --- Groups ------------------------------------------------------------------------------------------------------

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
    """{group: (cost account ids, how they were chosen, set in admin)} for cogs, lab, da, int and tax."""
    costs = {a: v for a, v in accounts.items() if v["category"] in ("cost_of_sales", "expenses")}

    def named(pattern):
        return {a for a, v in costs.items() if re.search(pattern, v["name"], re.I)}

    defaults = {
        "cogs": ({a for a, v in costs.items() if v["category"] == "cost_of_sales"}, "every Cost of sales account"),
        "lab": (named(LABOUR_WORDS), "accounts named wages, salaries, super, payroll, leave, Workcover or staff"),
        "int": (named(r"\binterest\b"), "cost accounts named interest"),
        "tax": (named(r"income tax"), "cost accounts named income tax"),
        "da": ({a for a, v in costs.items() if v["type"] == "DEPRECIATN"} | named(r"depreciat|amortis|amortiz"),
               "Depreciation accounts and those named depreciation or amortisation"),
    }
    chosen = {"cogs": settings.cogs_accounts, "lab": settings.labour_accounts, "int": settings.interest_accounts,
              "tax": settings.tax_accounts, "da": settings.depreciation_accounts}
    groups = {}
    for name, text in chosen.items():
        if text.strip():
            test = _codes(text)
            groups[name] = ({a for a, v in costs.items() if v["code"] and test(v["code"])}, f"set in admin: {text}",
                            True)
        else:
            groups[name] = defaults[name] + (False,)
    return groups


def classify(settings, accounts):
    """{account id: group} for every P&L account. Groups set in admin come first, then iLA's defaults, each in the
    order cost of sales, labour, D&A, interest, tax; so labour set in admin wins over the default cost of sales."""
    groups = account_groups(settings, accounts)
    out = {}
    for explicit in (True, False):
        for name in COST_GROUPS:
            ids, _, set_in_admin = groups[name]
            if set_in_admin == explicit:
                for account in ids:
                    out.setdefault(account, name)
    for account, info in accounts.items():
        if info["category"]:
            out.setdefault(account, _fallback(info["category"]))
    return out


def describe_groups(settings, accounts):
    """Lines saying which accounts make up each group chosen in settings, for the review's notes."""
    out = []
    for name, (ids, how, _) in account_groups(settings, accounts).items():
        codes = sorted(accounts[a]["code"] for a in ids if accounts[a]["code"])
        out.append(f"{GROUPS[name][0]}: {_ranges(codes) or 'none'} ({how})")
    return out


def _ranges(codes):
    """["6300", "6301", "6302", "6310"] -> "6300-6302, 6310"."""
    parts, run = [], []
    for code in codes:
        if run and code.isdigit() and run[-1].isdigit() and int(code) == int(run[-1]) + 1:
            run.append(code)
            continue
        if run:
            parts.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
        run = [code]
    if run:
        parts.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ", ".join(parts)


# --- Totals ------------------------------------------------------------------------------------------------------

def totals(lines, column):
    """{group: total, and "gp", "ebitda", "np"} for one column."""
    out = {g: ZERO for g in GROUPS}
    for line in lines:
        out[line.group] += line.values.get(column, ZERO)
    out["gp"] = out["rev"] - out["cogs"]
    out["ebitda"] = out["gp"] + out["oth"] - out["lab"] - out["opx"]
    out["np"] = out["ebitda"] - out["da"] - out["int"] - out["tax"]
    return out
