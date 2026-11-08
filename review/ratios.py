"""The review's ratios, as people write them in Review settings: ``numerator / denominator``, each side a sum of
account codes, ranges and groups, e.g.

    5110-5330 / 4005 + 4810 + 4830      cost of sales accounts over three revenue accounts
    lab / rev                           the Labour group over the Revenue group
    4000-4999 less 4810 / rev           "less" (or "minus") takes a term away

Terms: an account code (4005, "GL4005" is fine too), a range of codes (5110-5330; "-" always means a range), a group
(rev, cogs, oth, lab, opx, da, int, tax) or a subtotal (gp, ebitda, np). Terms are joined with +, a comma, or less.
Amounts are as Xero's P&L shows them: income and costs both positive.
"""

import re
from decimal import Decimal

ZERO = Decimal("0")
GROUP_TERMS = ("rev", "cogs", "oth", "lab", "opx", "da", "int", "tax", "gp", "ebitda", "np")
DEFAULTS = [("Gross profit %", "gp / rev"), ("COGS %", "cogs / rev"), ("Labour %", "lab / rev"),
            ("EBITDA %", "ebitda / rev"), ("Net profit %", "np / rev")]
CODE = r"[a-z0-9.]*[0-9][a-z0-9.]*"  # an account code has a digit, so a misspelt group isn't taken as one


class FormulaError(ValueError):
    pass


def parse(formula):
    """(numerator terms, denominator terms); a term is (sign, kind, value) with kind "group", "code" or "range"."""
    text = (formula or "").strip().lower()
    if not text:
        raise FormulaError("Write the ratio as numerator / denominator, e.g. 5110-5330 / 4005 + 4810 + 4830.")
    sides = text.split("/")
    if len(sides) != 2:
        raise FormulaError("Use one / between the numerator and the denominator, e.g. lab / rev.")
    return tuple(_side(side, name) for side, name in zip(sides, ("numerator", "denominator")))


def _side(text, name):
    text = re.sub(r"\bsum of\b|[()]", " ", text)
    text = re.sub(r"\bgl\s*(?=[0-9])", "", text)
    text = re.sub(r"\s*-\s*", "-", text.strip())
    parts = re.split(r"\s*(\+|,|\bless\b|\bminus\b)\s*", text)
    terms, sign = [], 1
    for i, part in enumerate(parts):
        part = part.strip()
        if i % 2:
            sign = -1 if part in ("less", "minus") else 1
            continue
        if not part:
            raise FormulaError(f"The {name} has an empty term: check for a doubled + or a missing code.")
        if part in GROUP_TERMS:
            terms.append((sign, "group", part))
        elif re.fullmatch(CODE, part):
            terms.append((sign, "code", part.upper()))
        elif re.fullmatch(rf"{CODE}-{CODE}", part):
            terms.append((sign, "range", part.upper()))
        elif "-" in part:
            raise FormulaError(f'"{part}": "-" makes a range of codes, e.g. 5110-5330; to take something away write '
                               f"less, e.g. rev less cogs.")
        else:
            raise FormulaError(f'"{part}" in the {name} isn\'t an account code, a range like 5110-5330 or one of '
                               f"{', '.join(GROUP_TERMS)}.")
    return terms


def _in_range(code, text):
    low, high = text.split("-", 1)
    if low.isdigit() and high.isdigit() and code.isdigit():
        return int(low) <= int(code) <= int(high)
    return low <= code <= high


def side_value(terms, lines, totals, column):
    """The sum of a side's terms for one column. ``lines``: review.checks.Line; ``totals``: totals(lines, column)."""
    value = ZERO
    for sign, kind, term in terms:
        if kind == "group":
            amount = totals[term]
        elif kind == "code":
            amount = sum((line.values[column] for line in lines if line.code.upper() == term), ZERO)
        else:
            amount = sum((line.values[column] for line in lines if line.code and _in_range(line.code.upper(), term)),
                         ZERO)
        value += sign * amount
    return value


def value(formula, lines, totals, column):
    """The ratio for one column (0.25 = 25%), or None when the denominator is nil."""
    numerator, denominator = parse(formula)
    bottom = side_value(denominator, lines, totals, column)
    if not bottom:
        return None
    return side_value(numerator, lines, totals, column) / bottom


def codes(formula):
    """The account codes and ranges a formula names, for checking them against the chart of accounts."""
    return [term for side in parse(formula) for _, kind, term in side if kind != "group"]
