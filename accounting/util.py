"""Dates and money formatting shared by the scan, rules and the Expectation Ledger."""

import calendar
from datetime import date
from decimal import Decimal

CENT = Decimal("0.01")


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def months_between(a, b):
    return (b.year - a.year) * 12 + b.month - a.month


def money(value):
    return f"${Decimal(value).quantize(Decimal('1')):,}"


def pct(value):
    return f"{value:.0%}"
