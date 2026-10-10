from datetime import date

import pytest

from console.parser import CommandSyntaxError, parse
from console.periods import FinancialYear, PeriodError, parse_period, window

TODAY = date(2026, 10, 10)
FY = FinancialYear(6, 30)


def test_parse_params_in_any_order_with_spaces():
    cmd = parse("/status --September 2026 --revenue --mtd")
    assert (cmd.name, cmd.text, cmd.params) == ("status", "", ["September 2026", "revenue", "mtd"])


def test_parse_text_input_and_output_files():
    cmd = parse('/pl --ytd >> "P&L ytd.xlsx" --Sep 2026')
    assert cmd.params == ["ytd", "Sep 2026"]
    assert cmd.output_file == "P&L ytd.xlsx"
    cmd = parse("/import journals << jnl.csv")
    assert (cmd.text, cmd.input_file) == ("journals", "jnl.csv")


def test_double_dash_inside_a_value_is_kept():
    assert parse("/inv INV--001").text == "INV--001"


def test_command_name_is_case_insensitive():
    assert parse("/STATUS").name == "status"


@pytest.mark.parametrize("line, message", [
    ("status", 'start with "/"'),
    ("/", "command name"),
    ("/status --", "Empty parameter"),
    ("/pl >> a.xlsx >> b.xlsx", "one output file"),
    ("/pl >>", "Name the output file"),
    ("/status%x", "Unknown command"),
])
def test_syntax_errors(line, message):
    with pytest.raises(CommandSyntaxError, match=message):
        parse(line)


@pytest.mark.parametrize("text, start, end", [
    ("September 2026", date(2026, 9, 1), date(2026, 9, 30)),
    ("sep 2026", date(2026, 9, 1), date(2026, 9, 30)),
    ("Sept-26", date(2026, 9, 1), date(2026, 9, 30)),
    ("2026-02", date(2026, 2, 1), date(2026, 2, 28)),
    ("02/2028", date(2028, 2, 1), date(2028, 2, 29)),
    ("FY2026", date(2025, 7, 1), date(2026, 6, 30)),
    ("fy27", date(2026, 7, 1), date(2027, 6, 30)),
    ("Q2 FY2027", date(2026, 10, 1), date(2026, 12, 31)),
    ("last month", date(2026, 9, 1), date(2026, 9, 30)),
    ("this month", date(2026, 10, 1), date(2026, 10, 31)),
    ("this quarter", date(2026, 10, 1), date(2026, 12, 31)),
    ("last quarter", date(2026, 7, 1), date(2026, 9, 30)),
    ("this fy", date(2026, 7, 1), date(2027, 6, 30)),
    ("last fy", date(2025, 7, 1), date(2026, 6, 30)),
    ("30/06/2026", date(2026, 6, 30), date(2026, 6, 30)),
    ("30 Jun 2026", date(2026, 6, 30), date(2026, 6, 30)),
    ("2026-06-30", date(2026, 6, 30), date(2026, 6, 30)),
    ("Jul 2026 to Sep 2026", date(2026, 7, 1), date(2026, 9, 30)),
])
def test_periods(text, start, end):
    period = parse_period(text, today=TODAY, fy=FY)
    assert (period.start, period.end) == (start, end)


def test_not_a_period():
    assert parse_period("revenue", today=TODAY, fy=FY) is None


def test_backwards_range_rejected():
    with pytest.raises(PeriodError):
        parse_period("Sep 2026 to Jul 2026", today=TODAY, fy=FY)


def test_december_year_end():
    fy = FinancialYear(12, 31)
    assert fy.bounds(TODAY) == (date(2026, 1, 1), date(2026, 12, 31))
    assert window("qtd", TODAY, fy).start == date(2026, 10, 1)


@pytest.mark.parametrize("kind, start", [("mtd", date(2026, 10, 1)), ("qtd", date(2026, 10, 1)),
                                         ("ytd", date(2026, 7, 1))])
def test_windows(kind, start):
    period = window(kind, TODAY, FY)
    assert (period.start, period.end) == (start, TODAY)


def test_capped_and_last_year():
    period = parse_period("October 2026", today=TODAY, fy=FY).capped(TODAY)
    assert period.end == TODAY
    assert period.last_year().start == date(2025, 10, 1)
    with pytest.raises(PeriodError):
        parse_period("November 2026", today=TODAY, fy=FY).capped(TODAY)
