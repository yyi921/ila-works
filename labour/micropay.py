"""Read a Micropay "employee breakdown" workbook: sheet *Micropay Breakdown*, one row per employee, cost account
and pay component, plus a *Summary* pivot with a Grand Total.

Only the columns below are read. Everything else in the file (date of birth, age, hire date, super fund, pay class,
addresses...) is never looked at, so it can't be stored.
"""

import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

SHEET = "Micropay Breakdown"
SUMMARY_SHEET = "Summary"
REQUIRED = ("Employee Code", "Full Name", "Period End Date", "Cost Account Code", "Transaction Type", "Amount")
OPTIONAL = ("Pay Period ID", "Pay Advice Print Date", "Pay Period Pay Frequency", "Employment Type",
            "Cost Account Description", "Pay Comp/Add Ded Code", "Pay Comp/Add Ded Desc", "Leave Start Date",
            "Leave End Date", "Hours")
EXCEL_EPOCH = date(1899, 12, 30)
CENT = Decimal("0.01")
FORTNIGHT = 14


class FileError(Exception):
    """The file can't be read as a pay run; the message says why, in plain English."""


@dataclass
class Line:
    employee_code: str
    employee_name: str
    employment_type: str
    cost_account_code: str
    cost_account_name: str
    transaction_type: str
    component_code: str
    component: str
    date_from: date
    date_to: date
    hours: Decimal
    amount: Decimal


@dataclass
class ParsedRun:
    pay_period_id: str
    period_end: date
    pay_date: date | None
    frequency: str
    lines: list
    summary_total: Decimal | None
    warnings: list = field(default_factory=list)

    @property
    def total(self):
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def period_start(self):
        return min(line.date_from for line in self.lines)


def to_date(value):
    """A cell as a date: a real date, an Excel serial number, or day-first text. None if empty or the 1899 blank."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        found = value.date()
    elif isinstance(value, date):
        found = value
    elif isinstance(value, (int, float)):
        found = EXCEL_EPOCH + timedelta(days=int(value))
    else:
        text = str(value).strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                found = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        else:
            return None
    return None if found <= EXCEL_EPOCH else found


def to_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value)).quantize(CENT)
    except InvalidOperation:
        raise FileError(f"{value!r} isn't a number.") from None


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read(data):
    """Parse the workbook bytes into a ParsedRun, or raise FileError."""
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001  (openpyxl raises many kinds for a file that isn't a workbook)
        raise FileError("This isn't an Excel workbook (.xlsx). Upload the Micropay employee breakdown as it "
                        "came.") from None
    if SHEET not in book.sheetnames:
        raise FileError(f'The workbook has no "{SHEET}" sheet. Its sheets: {", ".join(book.sheetnames)}.')
    rows = book[SHEET].iter_rows(values_only=True)
    headers = [_text(h) for h in next(rows, [])]
    index = {h: i for i, h in enumerate(headers) if h}
    missing = [h for h in REQUIRED if h not in index]
    if missing:
        raise FileError(f'The "{SHEET}" sheet is missing these columns: {", ".join(missing)}.')

    def cell(row, name):
        i = index.get(name)
        return row[i] if i is not None and i < len(row) else None

    raw, ids, ends, pay_dates, frequencies = [], set(), set(), set(), set()
    for n, row in enumerate(rows, 2):
        code = _text(cell(row, "Employee Code"))
        if not code:
            continue
        end = to_date(cell(row, "Period End Date"))
        if end is None:
            raise FileError(f"Row {n}: the Period End Date is missing or isn't a date.")
        ends.add(end)
        ids.add(_text(cell(row, "Pay Period ID")))
        if to_date(cell(row, "Pay Advice Print Date")):
            pay_dates.add(to_date(cell(row, "Pay Advice Print Date")))
        frequencies.add(_text(cell(row, "Pay Period Pay Frequency")))
        try:
            amount, hours = to_decimal(cell(row, "Amount")), to_decimal(cell(row, "Hours"))
        except FileError as exc:
            raise FileError(f"Row {n}: {exc}") from None
        raw.append((n, row, code, end, amount, hours))
    if not raw:
        raise FileError(f'The "{SHEET}" sheet has no employee lines.')
    if len(ends) > 1:
        raise FileError("The file has more than one Period End Date (" + ", ".join(
            f"{d:%d/%m/%Y}" for d in sorted(ends)) + "): upload one pay run per file.")
    period_end = ends.pop()
    frequency = next((f for f in frequencies if f), "")
    default_start = period_end - timedelta(days=FORTNIGHT - 1)

    warnings, lines = [], []
    undated = 0
    for n, row, code, end, amount, hours in raw:
        first, last = to_date(cell(row, "Leave Start Date")), to_date(cell(row, "Leave End Date"))
        if first is None and last is None:
            first, last = default_start, period_end
            undated += 1
        first, last = first or last, last or first
        if last < first:
            raise FileError(f"Row {n}: Leave End Date is before Leave Start Date.")
        lines.append(Line(
            employee_code=code, employee_name=_text(cell(row, "Full Name")),
            employment_type=_text(cell(row, "Employment Type")),
            cost_account_code=_text(cell(row, "Cost Account Code")) or "(none)",
            cost_account_name=_text(cell(row, "Cost Account Description")),
            transaction_type=_text(cell(row, "Transaction Type")),
            component_code=_text(cell(row, "Pay Comp/Add Ded Code")),
            component=_text(cell(row, "Pay Comp/Add Ded Desc")),
            date_from=first, date_to=last, hours=hours, amount=amount))
    if undated:
        warnings.append(f"{undated} line(s) had no dates; they're spread over the fortnight to "
                        f"{period_end:%d/%m/%Y}.")
    run = ParsedRun(pay_period_id=next((i for i in ids if i), ""), period_end=period_end,
                    pay_date=max(pay_dates) if pay_dates else None, frequency=frequency, lines=lines,
                    summary_total=_summary_total(book), warnings=warnings)
    if run.summary_total is not None and run.summary_total != run.total:
        warnings.append(f"The lines add up to {run.total:,.2f} but the Summary sheet's Grand Total is "
                        f"{run.summary_total:,.2f}. Check the Summary isn't filtered.")
    return run


def _summary_total(book):
    if SUMMARY_SHEET not in book.sheetnames:
        return None
    for row in book[SUMMARY_SHEET].iter_rows(values_only=True):
        if row and _text(row[0]).lower() == "grand total" and len(row) > 1 and row[1] not in (None, ""):
            try:
                return to_decimal(row[1])
            except FileError:
                return None
    return None
