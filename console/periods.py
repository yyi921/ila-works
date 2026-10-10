"""Reading periods typed in commands ("September 2026", "FY2026", "Q1 FY27", "30/06/2026", "last month") and
working out month-, quarter- and year-to-date windows against the organisation's financial year."""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS |= {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
MONTHS["sept"] = 9


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class FinancialYear:
    """Financial year end as Xero holds it (e.g. 30 June: end_month=6, end_day=30)."""

    end_month: int = 6
    end_day: int = 30

    def _end_in(self, year):
        return date(year, self.end_month, min(self.end_day, calendar.monthrange(year, self.end_month)[1]))

    def bounds(self, day):
        """(start, end) of the financial year containing ``day``."""
        end = self._end_in(day.year)
        if day > end:
            end = self._end_in(day.year + 1)
        return self._end_in(end.year - 1) + timedelta(days=1), end

    def year(self, label_year):
        """(start, end) of the financial year ending in ``label_year`` (FY2026 ends in 2026)."""
        end = self._end_in(label_year)
        return self._end_in(label_year - 1) + timedelta(days=1), end

    def quarter_start(self, day):
        start, _ = self.bounds(day)
        months = (day.year - start.year) * 12 + day.month - start.month
        return add_months(date(start.year, start.month, 1), months // 3 * 3)


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    @property
    def is_single_day(self):
        return self.start == self.end

    def capped(self, today):
        """The period with its end brought back to ``today`` if it runs into the future."""
        if self.end <= today:
            return self
        if self.start > today:
            raise PeriodError(f"{self.label} hasn't started yet.")
        return Period(self.start, today, f"{self.label} to {fmt(today)}")

    def last_year(self):
        return Period(shift_year(self.start, -1), shift_year(self.end, -1), f"{self.label}, last year")


def fmt(day):
    return f"{day.day} {calendar.month_abbr[day.month]} {day.year}"


def add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def shift_year(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def month_period(year, month):
    last = calendar.monthrange(year, month)[1]
    return Period(date(year, month, 1), date(year, month, last), f"{calendar.month_name[month]} {year}")


def _year(text):
    year = int(text)
    return year + 2000 if year < 100 else year


def parse_date(text):
    """A single date: 30/06/2026 (day first, Australian style), 2026-06-30, 30 Jun 2026, 30-Jun-26."""
    text = text.strip()
    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y",
                    "%d %b %y", "%d %B %y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _month(text):
    """September 2026, Sep 2026, Sep-26, 2026-09, 09/2026."""
    m = re.fullmatch(r"([a-z]+)[\s\-]+(\d{2}|\d{4})", text)
    if m and m.group(1) in MONTHS:
        return month_period(_year(m.group(2)), MONTHS[m.group(1)])
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text) or re.fullmatch(r"(\d{1,2})/(\d{4})", text)
    if m:
        year, month = (m.group(1), m.group(2)) if len(m.group(1)) == 4 else (m.group(2), m.group(1))
        if 1 <= int(month) <= 12:
            return month_period(int(year), int(month))
    return None


def parse_period(text, *, today, fy):
    """Read one period. Returns a Period, or None if ``text`` isn't a period at all."""
    text = re.sub(r"\s+", " ", text.strip().lower())
    if text in ("today", "now"):
        return Period(today, today, fmt(today))
    if text == "yesterday":
        day = today - timedelta(days=1)
        return Period(day, day, fmt(day))
    if text in ("this month", "month"):
        return month_period(today.year, today.month)
    if text == "last month":
        prev = add_months(date(today.year, today.month, 1), -1)
        return month_period(prev.year, prev.month)
    if text in ("this quarter", "last quarter"):
        start = fy.quarter_start(today)
        if text == "last quarter":
            start = add_months(start, -3)
        end = add_months(start, 3) - timedelta(days=1)
        return Period(start, end, f"Quarter {fmt(start)} to {fmt(end)}")
    if text in ("this fy", "this year", "last fy", "last year"):
        start, end = fy.bounds(today)
        if text.startswith("last"):
            start, end = fy.bounds(start - timedelta(days=1))
        return Period(start, end, f"FY{end.year}")

    m = re.fullmatch(r"fy ?(\d{2}|\d{4})", text)
    if m:
        start, end = fy.year(_year(m.group(1)))
        return Period(start, end, f"FY{end.year}")
    m = re.fullmatch(r"q([1-4]) ?fy ?(\d{2}|\d{4})", text)
    if m:
        fy_start, _ = fy.year(_year(m.group(2)))
        start = add_months(fy_start, (int(m.group(1)) - 1) * 3)
        end = add_months(start, 3) - timedelta(days=1)
        return Period(start, end, f"Q{m.group(1)} FY{_year(m.group(2))}")

    if " to " in text:
        left, right = text.split(" to ", 1)
        first, last = parse_period(left, today=today, fy=fy), parse_period(right, today=today, fy=fy)
        if not first or not last:
            return None
        if last.end < first.start:
            raise PeriodError(f"{right} is before {left}.")
        return Period(first.start, last.end, f"{fmt(first.start)} to {fmt(last.end)}")

    month = _month(text)
    if month:
        return month
    day = parse_date(text)
    if day:
        return Period(day, day, fmt(day))
    return None


def window(kind, reference, fy):
    """The month-, quarter- or year-to-date window ending on ``reference``."""
    if kind == "mtd":
        start, label = date(reference.year, reference.month, 1), "MTD"
    elif kind == "qtd":
        start, label = fy.quarter_start(reference), "QTD"
    elif kind == "ytd":
        start, label = fy.bounds(reference)[0], "YTD"
    else:
        raise ValueError(kind)
    return Period(start, reference, f"{label} to {fmt(reference)}")
