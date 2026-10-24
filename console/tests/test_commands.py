from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from console.output import Figures, Note, Table
from console.parser import parse
from console.periods import PeriodError
from console.registry import CommandError, Context, lookup

from .fake_xero import fake_source

D = Decimal
TODAY = date(2026, 10, 10)


def run(line, source=None):
    source = source or fake_source()
    org = SimpleNamespace(name="Demo Events", timezone="Australia/Brisbane", pk=1)
    ctx = Context(org, user=None, source_factory=lambda _: source, today_override=TODAY)
    cmd = parse(line)
    return lookup(cmd.name).func(ctx, cmd)


def figures(result):
    return {f.label: f for b in result.blocks if isinstance(b, Figures) for f in b.items}


def table(result, index=0):
    return [b for b in result.blocks if isinstance(b, Table)][index]


def rows_by_label(tbl, column=0):
    return {row[column]: row for row in tbl.rows}


def notes(result):
    return " ".join(b.text for b in result.blocks if isinstance(b, Note))


# --- /status ---


def test_status_snapshot():
    result = run("/status")
    f = figures(result)
    assert f["Cash at bank"].value == D("14500.00")
    assert f["Revenue MTD"].value == D("10000.00")
    assert f["Revenue MTD"].detail == "budget 4,000.00, last year 8,000.00"  # 12,400 x 10/31 days
    assert f["Revenue YTD"].detail == "budget 38,500.00, last year 8,000.00"  # Jul + Aug + Sep + 10/31 of Oct
    assert f["Debtors"].value == D("7300.00")
    # 300 overdue + 1,000 AUD + 650 USD at 0.65
    assert f["Bills due by 17 Oct 2026"].value == D("2300.00")
    assert f["Bills due by 17 Oct 2026"].detail == "3 bills, including 300.00 overdue"
    assert "include 300.00 on 1 bill due before 10 Oct 2026" in notes(result)
    assert f["Unreconciled Xero transactions"].value == "3"
    assert f["Unreconciled Xero transactions"].detail == "oldest 19 Sep 2026; bank feed lines not included"


def test_status_snapshot_without_new_permissions_still_answers():
    old_scopes = "accounting.reports.profitandloss.read accounting.invoices.read accounting.settings.read"
    result = run("/status", fake_source(old_scopes))
    f = figures(result)
    assert "Cash at bank" not in f and "Debtors" not in f
    assert f["Revenue MTD"].detail == "last year 8,000.00"
    assert "Reconnect with /connect" in notes(result)


def test_status_revenue_for_a_past_month():
    result = run("/status --September 2026 --revenue --mtd")
    [row] = table(result).rows
    assert row[:2] == ["Revenue", "MTD to 30 Sep 2026"]
    assert row[2:] == [D("10000.00"), D("12000.00"), D("-2000.00"), D("-2000") / D("12000"),
                       D("8000.00"), D("2000.00"), D("0.25")]


def test_status_mtd_and_ytd_together():
    result = run("/status --revenue --mtd --ytd")
    rows = table(result).rows
    assert [r[1] for r in rows] == ["MTD to 10 Oct 2026", "YTD to 10 Oct 2026"]
    assert [r[3] for r in rows] == [D("4000.00"), D("38500.00")]
    assert "pro-rated" in notes(result)


def test_status_all_measures_tie_to_net_profit():
    result = run("/status --September 2026")
    by_measure = rows_by_label(table(result))
    assert by_measure["Revenue"][2] == D("10000.00")
    assert by_measure["Gross profit"][2] == D("6000.00")
    # Expenses include the 50.00 currency loss that isn't on an account, so GP + other income - expenses = NP.
    assert by_measure["Expenses"][2] == D("4050.00")
    assert by_measure["Net profit"][2] == D("2450.00")
    assert by_measure["Expenses"][4] == D("4050.00") * -1  # favourable when below budget (budget 0 in Sep)


def test_status_rejects_unknown_params():
    with pytest.raises(CommandError, match='doesn\'t understand "--revnue"'):
        run("/status --revnue")


def test_status_rejects_future_period():
    with pytest.raises(PeriodError, match="hasn't started"):
        run("/status --December 2026")


# --- /pl ---


def test_pl_by_account_with_budget_and_last_year():
    result = run("/pl --September 2026")
    assert result.title == "Profit and loss: September 2026"
    rows = rows_by_label(table(result), column=1)
    assert rows["Sales"][2:] == [D("10000.00"), D("12000.00"), D("-2000.00"), D("8000.00"), D("2000.00")]
    assert rows["Wages"][2:5] == [D("3000.00"), D("0"), D("-3000.00")]
    assert rows["Gross profit"][2] == D("6000.00")
    assert rows["Other items not linked to an account (e.g. currency gains/losses)"][2] == D("50.00")
    assert rows["Net profit"][2:4] == [D("2450.00"), D("12000.00")]
    assert "Bank" not in " ".join(str(r[1]) for r in table(result).rows)  # balance sheet budget lines ignored


def test_pl_windows_are_exclusive():
    with pytest.raises(CommandError, match="only one"):
        run("/pl --mtd --ytd")


# --- Balance sheet, trial balance, cash ---


def test_cash_and_bs():
    rows = rows_by_label(table(run("/cash --as at 30/09/2026")))
    assert rows["Total"][1] == D("14500.00")
    result = run("/bs --FY2026")
    assert result.title == "Balance sheet as at 30 Jun 2026"


def test_as_at_and_period_conflict():
    with pytest.raises(CommandError, match="not both"):
        run("/bs --as at 30/06/2026 --FY2026")


def test_tb_columns():
    tbl = table(run("/tb"))
    assert [c.label for c in tbl.columns] == ["Section", "Account", "Debit", "Credit", "YTD Debit", "YTD Credit"]
    assert tbl.rows[0][3] == D("10000.00")


# --- Debtors, creditors, bills ---


def test_debtors_ageing():
    result = run("/debtors")
    rows = rows_by_label(table(result))
    # columns: contact, Current, 1-30, 31-60, 61-90, 90+, total
    assert rows["Smith Wedding"][1:] == [D("4000.00"), D("1000.00"), 0, 0, 0, D("5000.00")]
    assert rows["Expo Pty Ltd"][5] == D("2000.00")
    assert rows["Total"][6] == D("7000.00")
    assert "300.00 difference" in notes(result)


def test_debtors_detail_and_top():
    assert len(table(run("/debtors --detail")).rows) == 4
    rows = table(run("/debtors --top 1")).rows
    assert [r[0] for r in rows] == ["Smith Wedding", "Total (all contacts)"]


def test_creditors_ageing_converts_foreign_currency():
    result = run("/creditors")
    rows = rows_by_label(table(result))
    assert rows["US Audio"][1] == D("1000.00")
    assert rows["Acme Foods"][2] == D("300.00")
    assert rows["Total"][6] == D("2550.00")
    assert "own exchange rate" in notes(result)


@pytest.mark.parametrize("line, numbers, total", [
    ("/bills", ["B-1", "B-2", "B-3"], D("2300.00")),
    ("/bills --overdue", ["B-1"], D("300.00")),
    ("/bills --due 60", ["B-1", "B-2", "B-3", "B-4"], D("2550.00")),
])
def test_bills(line, numbers, total):
    tbl = table(run(line))
    assert [r[2] for r in tbl.rows[:-1]] == numbers
    assert tbl.rows[-1][5] == total


def test_bills_due_must_be_a_number():
    with pytest.raises(CommandError, match="whole number"):
        run("/bills --due soon")


# --- Recon, lookups, help ---


def test_reconcile_bank():
    rows = rows_by_label(table(run("/reconcile --bank")))
    assert rows["Business Bank"][1:] == [3, D("300.00"), date(2026, 9, 19)]
    assert rows["Savings"][1:] == [0, D("0"), None]


def test_inv():
    result = run("/inv INV-1")
    assert result.title == "Invoice INV-1"
    assert figures(result)["Amount due"].value == D("4000.00")
    assert table(result).rows[0][0] == "Venue hire"
    with pytest.raises(CommandError, match="No invoice, bill or credit note"):
        run("/inv INV-999")


def test_find():
    result = run("/find smith")
    assert table(result, 0).rows[0][0] == "Smith Wedding"
    assert table(result, 1).rows[0][0] == "INV-1"


def test_help_lists_and_explains():
    assert any(r[0].startswith("/status") for r in table(run("/help")).rows)
    assert run("/help status").title == "/status"
    assert run("/help ar").title == "/debtors"
    with pytest.raises(CommandError):
        run("/help nothing")


def test_snapshot_uses_cache_for_repeated_report():
    source = fake_source()
    run("/status", source)
    balance_sheet_calls = [c for c in source.client.calls if c[0] == "Reports/BalanceSheet"]
    assert len(balance_sheet_calls) == 1  # cash and debtors share one call


def test_clean_is_listed_and_harmless_on_the_server():
    assert run("/help clean").title == "/clean"
    assert run("/cls").title == "Nothing to clear"


# --- /compare ---


def test_compare_bill_scan_flags_spike_new_and_missing():
    result = run("/compare --bill")
    assert result.title == "Bills in Sep 2026 against the previous 12 months"
    rows = table(result).rows
    # Sorted: High, New, Missing, then unflagged. Water Board (quarterly, nothing in Sep) is left out.
    assert [r[0] for r in rows] == ["Origin Energy", "Smart Consulting", "Rent Co", "US Audio", "Total"]
    origin, smart, rent, audio, total = rows
    # 11 x 800 + (800 - 100 credit note) = 9,500 over 12 months
    assert origin[1:4] == [D("1700.00"), D("791.67"), D("908.33")] and origin[5:] == ["12/12", "High"]
    assert smart[1] == D("5000.00") and smart[6] == "New"  # 5,500 GST-inclusive less 500 GST
    assert rent[1:4] == [D("0"), D("2000.00"), D("-2000.00")] and rent[6] == "Missing"
    assert audio[1:3] == [D("1000.00"), D("1000.00")] and audio[6] == ""  # 650 USD at 0.65
    # Sep total 7,700; 12-month average (9,500 + 24,000 + 1,200 + 1,000) / 12 = 2,975
    assert total[1:4] == [D("7700.00"), D("2975.00"), D("4725.00")]
    assert "3 flagged" in notes(result)


def test_compare_bill_scan_by_account():
    rows = rows_by_label(table(run("/compare --bill --by account")))
    power = rows["445 Light, Power, Heating"]
    # Origin 9,500 + Water Board 1,200 over 12 months = 891.67 a month
    assert power[1:3] == [D("1700.00"), D("891.67")] and power[6] == "High"
    assert rows["412 Consulting"][6] == "New" and rows["469 Rent"][6] == "Missing"


def test_compare_supplier_month_by_month():
    result = run("/compare --bill --supplier origin")
    assert result.title == "Bills month by month: Origin Energy"
    rows = rows_by_label(table(result))
    assert list(rows)[0] == "Oct 2025" and "Oct 2026 (1–10)" in rows
    sep = rows["Sep 2026"]
    assert sep[1:3] == [D("1700.00"), D("1000.00")] and sep[6] == "High"
    assert rows["Aug 2026"][1] == D("700.00") and rows["Aug 2026"][6] == ""
    assert rows["Oct 2026 (1–10)"][6] == ""  # still in progress: not missing
    assert rows["Usual month"][1] == D("866.67")  # (10 x 800 + 700 + 1,700) / 12
    assert "in progress" in notes(result)


def test_compare_supplier_by_code():
    assert run("/compare --supplier ORIG").title == "Bills month by month: Origin Energy"


def test_compare_one_bill_against_the_year_before():
    rows = table(run("/compare --bill OE-0912")).rows
    assert rows[0][0] == "Oct 2025" and rows[11][0] == "Sep 2026"
    assert rows[11][7] == "OE-0912 ◀" and rows[11][6] == "High"


def test_compare_account_with_detail():
    result = run("/compare --bill --account consulting --detail")
    assert result.title == "Bills month by month: 412 Consulting"
    assert rows_by_label(table(result))["Sep 2026"][6] == "New"
    [doc] = table(result, 1).rows
    assert doc[1:] == ["SC-1", "Bill", "Smart Consulting", "412", D("5000.00")]


def test_compare_measures_use_monthly_columns():
    source = fake_source()
    result = run("/compare --revenue", source)
    rows = rows_by_label(table(result))
    assert rows["Sep 2026"][1] == D("10000.00") and rows["Oct 2025"][1] == D("8900.00")  # newest column first
    assert rows["Oct 2026 (1–10)"][1] == D("10000.00")
    assert rows["Usual month"][1] == D("9450.00")
    monthly = [c for c in source.client.calls if c[0] == "Reports/ProfitAndLoss" and c[1].get("timeframe") == "MONTH"]
    assert len(monthly) == 1 and monthly[0][1]["periods"] == 11 and monthly[0][1]["fromDate"] == "2026-09-01"


@pytest.mark.parametrize("line, message", [
    ("/compare", "Say what to compare"),
    ("/compare --bill --invoice", "not both"),
    ("/compare --bill --revenue", "not both"),
    ("/compare --bill --supplier nobody", 'No bills from a supplier matching "nobody"'),
    ("/compare --bill --Jul 2026 to Sep 2026", "A scan checks one month"),
    ("/compare --bill --min lots", "isn't an amount"),
])
def test_compare_errors(line, message):
    with pytest.raises(CommandError, match=message):
        run(line)


def test_compare_without_credit_note_access_warns(monkeypatch):
    from xero.api import XeroScopeMissing
    source = fake_source()
    real = source.client.get_all

    def get_all(path, collection, params=None):
        if path == "CreditNotes":
            raise XeroScopeMissing("no access")
        return real(path, collection, params)

    monkeypatch.setattr(source.client, "get_all", get_all)
    result = run("/compare --bill", source)
    assert rows_by_label(table(result))["Origin Energy"][2] == D("800.00")  # no 100.00 credit note
    assert "Credit notes aren't included" in notes(result)


@pytest.mark.parametrize("line", ["/compare --bill Origin Energy", "/compare --bill origin", "/compare --bill ORIG"])
def test_compare_bill_takes_a_supplier_name_or_code(line):
    result = run(line)
    assert result.title == "Bills month by month: Origin Energy"
    assert rows_by_label(table(result))["Sep 2026"][6] == "High"


def test_compare_bill_unknown_number_or_supplier():
    with pytest.raises(CommandError, match='No bill numbered "Nobody Pty", and no bills from a supplier'):
        run("/compare --bill Nobody Pty")
    with pytest.raises(CommandError, match="not both"):
        run("/compare --bill OE-0912 --supplier Origin")


def test_compare_month_by_month_lists_document_numbers_for_inv():
    result = run("/compare --bill origin")
    tbl = table(result)
    assert tbl.columns[-1].label == "Bills"
    rows = rows_by_label(tbl)
    assert rows["Sep 2026"][7] == "OE-0912"
    assert rows["Aug 2026"][7] == "OE-2608, OE-CN1"  # the bill and the credit note
    assert "/inv OE-0912" in notes(result)


def test_inv_opens_a_credit_note():
    result = run("/inv OE-CN1")
    assert result.title == "Supplier credit note OE-CN1"
    f = figures(result)
    assert f["Credit used"].value == D("80.00") and f["Credit remaining"].value == D("30.00")
    assert "Due" not in f
    assert table(result).rows[0][0] == "Billing error refund"
