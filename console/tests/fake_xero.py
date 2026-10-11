"""A small simulated Xero organisation in the shape of the Accounting API's JSON, for testing the source layer and
commands without network calls. Today is 10 October 2026; the financial year ends 30 June."""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from xero.api import XeroClient
from xero.source import XeroSource

ALL_SCOPES = ("accounting.reports.profitandloss.read accounting.reports.trialbalance.read "
              "accounting.reports.balancesheet.read accounting.budgets.read accounting.invoices.read "
              "accounting.payments.read accounting.banktransactions.read accounting.settings.read "
              "accounting.contacts.read")

ACCOUNTS = [
    ("a-sales", "200", "Sales", "REVENUE", ""),
    ("a-other", "260", "Other Revenue", "OTHERINCOME", ""),
    ("a-cogs", "310", "Cost of Goods Sold", "DIRECTCOSTS", ""),
    ("a-wages", "477", "Wages", "EXPENSE", ""),
    ("a-rent", "469", "Rent", "OVERHEADS", ""),
    ("a-cons", "412", "Consulting", "EXPENSE", ""),
    ("a-power", "445", "Light, Power, Heating", "OVERHEADS", ""),
    ("a-bank", "090", "Business Bank", "BANK", ""),
    ("a-save", "091", "Savings", "BANK", ""),
    ("a-ar", "610", "Accounts Receivable", "CURRENT", "DEBTORS"),
    ("a-ap", "800", "Accounts Payable", "CURRLIAB", "CREDITORS"),
]


def _cell(value, account=None):
    cell = {"Value": value}
    if account:
        cell["Attributes"] = [{"Value": account, "Id": "account"}]
    return cell


def _row(label, account, *values):
    return {"RowType": "Row", "Cells": [_cell(label, account)] + [_cell(v, account) for v in values]}


def _section(title, rows, total=None):
    section = {"RowType": "Section", "Title": title, "Rows": rows}
    if total:
        section["Rows"] = rows + [{"RowType": "SummaryRow", "Cells": [_cell(c) for c in total]}]
    return section


# Profit and loss by period (fromDate, toDate): current, last year. Net profit includes a 50.00 currency loss
# that isn't on an account row.
PL = {
    "default": {
        "a-sales": ("10000.00", "8000.00"), "a-other": ("500.00", ""), "a-cogs": ("4000.00", "3000.00"),
        "a-wages": ("3000.00", "2500.00"), "a-rent": ("1000.00", "1000.00"), "np": ("2450.00", "1500.00"),
    },
}


def profit_and_loss(params):
    data = PL.get((params["fromDate"], params["toDate"]), PL["default"])
    periods = int(params.get("periods") or 0)
    if params.get("timeframe") == "MONTH":  # newest month first; sales fall by 100 a month going back
        columns = periods + 1

        def vals(key):
            base = data[key][0]
            if key == "a-sales":
                return tuple(f"{Decimal(base) - 100 * i:.2f}" for i in range(columns))
            return (base,) * columns
    else:
        columns = 2 if periods else 1

        def vals(key):
            return data[key][:columns]

    return {"Reports": [{"ReportID": "ProfitAndLoss", "Rows": [
        {"RowType": "Header", "Cells": [_cell("")] + [_cell(f"col{i}") for i in range(columns)]},
        _section("Income", [_row("Sales", "a-sales", *vals("a-sales"))], ["Total Income", *vals("a-sales")]),
        _section("Other Income", [_row("Other Revenue", "a-other", *vals("a-other"))]),
        _section("Less Cost of Sales", [_row("Cost of Goods Sold", "a-cogs", *vals("a-cogs"))]),
        _section("", [_row("Gross Profit", None, "6000.00", "5000.00")]),
        _section("Less Operating Expenses", [_row("Wages", "a-wages", *vals("a-wages")),
                                             _row("Rent", "a-rent", *vals("a-rent"))]),
        _section("", [_row("Net Profit", None, *vals("np"))]),
    ]}]}


def balance_sheet(params):
    return {"Reports": [{"ReportID": "BalanceSheet", "Rows": [
        {"RowType": "Header", "Cells": [_cell(""), _cell(params["date"])]},
        _section("Assets", []),
        _section("Bank", [_row("Business Bank", "a-bank", "15000.00"), _row("Savings", "a-save", "-500.00")],
                 ["Total Bank", "14500.00"]),
        _section("Current Assets", [_row("Accounts Receivable", "a-ar", "7300.00")]),
        _section("Current Liabilities", [_row("Accounts Payable", "a-ap", "2200.00")]),
    ]}]}


BILLS = [  # number, supplier, due date, amount due, currency, rate
    ("B-1", "Acme Foods", "2026-10-05T00:00:00", "300.00", "AUD", "1"),
    ("B-2", "Hall Hire", "2026-10-12T00:00:00", "1000.00", "AUD", "1"),
    ("B-3", "US Audio", "2026-10-15T00:00:00", "650.00", "USD", "0.65"),
    ("B-4", "Later Co", "2026-11-30T00:00:00", "250.00", "AUD", "1"),
]
INVOICES = [  # number, customer, due date, amount due
    ("INV-1", "Smith Wedding", "2026-10-20T00:00:00", "4000.00"),
    ("INV-2", "Smith Wedding", "2026-09-25T00:00:00", "1000.00"),
    ("INV-3", "Expo Pty Ltd", "2026-06-30T00:00:00", "2000.00"),
]


def _months(first, last):
    """First day of each month from first to last."""
    y, m = first
    while (y, m) <= last:
        yield date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# Bills over time: (number, supplier, supplier code, date, account code, line amount, tax, line amount types,
# currency, rate). Origin Energy is monthly with a spike in Sep 2026; Rent Co is monthly but missing in Sep 2026;
# Smart Consulting is new in Sep 2026 (GST-inclusive lines); Water Board is quarterly; US Audio bills in USD.
HISTORY = (
    [(f"OE-{d:%y%m}", "Origin Energy", "ORIG", d.replace(day=12), "445", "800.00", "80.00", "Exclusive", "AUD", "1")
     for d in _months((2025, 9), (2026, 8))]
    + [("OE-0912", "Origin Energy", "ORIG", date(2026, 9, 12), "445", "1700.00", "170.00", "Exclusive", "AUD", "1")]
    + [(f"RC-{d:%y%m}", "Rent Co", "", d, "469", "2000.00", "200.00", "Exclusive", "AUD", "1")
       for d in _months((2025, 9), (2026, 8))]
    + [("SC-1", "Smart Consulting", "", date(2026, 9, 20), "412", "5500.00", "500.00", "Inclusive", "AUD", "1")]
    + [(f"WB-{d:%y%m}", "Water Board", "", d.replace(day=5), "445", "300.00", "30.00", "Exclusive", "AUD", "1")
       for d in (date(2025, 11, 1), date(2026, 2, 1), date(2026, 5, 1), date(2026, 8, 1))]
    + [("UA-3", "US Audio", "", date(2026, 3, 3), "310", "650.00", "0.00", "Exclusive", "USD", "0.65"),
       ("UA-9", "US Audio", "", date(2026, 9, 3), "310", "650.00", "0.00", "Exclusive", "USD", "0.65")]
)
# Credit notes from suppliers: Origin Energy refunded 100.00 in Aug 2026.
CREDIT_HISTORY = [("OE-CN1", "Origin Energy", "ORIG", date(2026, 8, 20), "445", "100.00", "10.00", "Exclusive", "AUD", "1")]


def _date_range(where):
    def part(marker):
        y, m, d = (int(x) for x in where.split(marker)[1].split(")")[0].split(","))
        return date(y, m, d)
    return part("Date>=DateTime("), part("Date<=DateTime(")


def _history(rows, where, number_field, kind):
    assert 'Status!="DRAFT"' in where and 'Status!="SUBMITTED"' in where
    start, end = _date_range(where)
    return [{number_field: n, "Type": kind, "Contact": {"Name": c, "AccountNumber": code},
             "Date": f"/Date({int((d - date(1970, 1, 1)).total_seconds()) * 1000}+0000)/",
             "LineAmountTypes": types, "CurrencyCode": cur, "CurrencyRate": rate,
             "LineItems": [{"AccountCode": acc, "Description": "", "LineAmount": amt, "TaxAmount": tax}]}
            for n, c, code, d, acc, amt, tax, types, cur, rate in rows if start <= d <= end]


RECENT_SALES = [{
    "InvoiceID": "i-2070", "InvoiceNumber": "INV-2070", "Type": "ACCREC", "Status": "PAID",
    "Contact": {"ContactID": "c-harbour", "Name": "Harbour Events Pty Ltd"}, "Reference": "CELTIC NIGHTS 2026",
    "DateString": "2026-04-30T00:00:00", "LineAmountTypes": "Exclusive",
    "LineItems": [
        {"Description": "Venue Hire - Arena", "AccountCode": "200", "TaxType": "OUTPUT", "UnitAmount": "4400.00"},
        {"Description": "Security", "AccountCode": "260", "TaxType": "OUTPUT", "UnitAmount": "85.00",
         "Tracking": [{"Name": "Event", "Option": "Celtic Nights"}]},
        {"Description": "First Aid", "AccountCode": "260", "TaxType": "EXEMPTOUTPUT", "UnitAmount": "100.00"},
    ]}, {
    "InvoiceID": "i-2069", "InvoiceNumber": "INV-2069", "Type": "ACCREC", "Status": "DRAFT",
    "Contact": {"ContactID": "c-smith", "Name": "Smith Wedding"}, "Reference": "Deposit",
    "DateString": "2026-04-20T00:00:00", "LineAmountTypes": "Inclusive", "LineItems": []}]


def invoices(params):
    where = params.get("where", "")
    if params.get("order") == "Date DESC":
        return {"Invoices": RECENT_SALES}
    if "Date>=DateTime(" in where:
        if 'Type=="ACCPAY"' in where:
            return {"Invoices": _history(HISTORY, where, "InvoiceNumber", "ACCPAY")}
        return {"Invoices": []}
    if params.get("InvoiceNumbers") == "OE-0912":
        return {"Invoices": [{"InvoiceID": "oe-0912", "InvoiceNumber": "OE-0912", "Type": "ACCPAY",
                              "Status": "PAID", "Contact": {"Name": "Origin Energy"},
                              "DateString": "2026-09-12T00:00:00", "LineItems": [{"LineAmount": "1700.00"}]}]}
    if "InvoiceNumbers" in params:
        if params["InvoiceNumbers"] != "INV-1":
            return {"Invoices": []}
        return {"Invoices": [{
            "InvoiceID": "i-1", "InvoiceNumber": "INV-1", "Type": "ACCREC", "Status": "AUTHORISED",
            "Contact": {"Name": "Smith Wedding"}, "DateString": "2026-09-20T00:00:00",
            "DueDateString": "2026-10-20T00:00:00", "Reference": "Deposit", "CurrencyCode": "AUD",
            "SubTotal": "4000.00", "TotalTax": "400.00", "Total": "4400.00", "AmountPaid": "400.00",
            "AmountDue": "4000.00",
            "LineItems": [{"Description": "Venue hire", "AccountCode": "200", "Quantity": "1.0000",
                           "UnitAmount": "4000.00", "LineAmount": "4000.00"}],
        }]}
    if 'Type=="ACCPAY"' in where:
        rows = BILLS
        if "DueDate<=DateTime(" in where:
            y, m, d = (int(x) for x in where.split("DueDate<=DateTime(")[1].rstrip(")").split(","))
            limit = f"{y:04d}-{m:02d}-{d:02d}"
            rows = [b for b in BILLS if b[2][:10] <= limit]
        return {"Invoices": [{"InvoiceNumber": n, "Contact": {"Name": c}, "DueDateString": due, "AmountDue": amt,
                              "CurrencyCode": cur, "CurrencyRate": rate, "DateString": "2026-09-01T00:00:00"}
                             for n, c, due, amt, cur, rate in rows]}
    if 'Type=="ACCREC"' in where:
        return {"Invoices": [{"InvoiceNumber": n, "Contact": {"Name": c}, "DueDateString": due, "AmountDue": amt,
                              "CurrencyCode": "AUD", "CurrencyRate": "1", "DateString": "2026-06-01T00:00:00"}
                             for n, c, due, amt in INVOICES]}
    if "Contains" in where:
        return {"Invoices": [{"InvoiceNumber": "INV-1", "Type": "ACCREC", "Contact": {"Name": "Smith Wedding"},
                              "DateString": "2026-09-20T00:00:00", "Status": "AUTHORISED", "Total": "4400.00",
                              "AmountDue": "4000.00"}]}
    raise AssertionError(f"unexpected Invoices query {params}")


def route(path, params):
    if path == "Organisation":
        return {"Organisations": [{"Name": "Demo Events", "BaseCurrency": "AUD", "FinancialYearEndDay": 30,
                                   "FinancialYearEndMonth": 6}]}
    if path == "Accounts":
        return {"Accounts": [{"AccountID": i, "Code": c, "Name": n, "Type": t, "SystemAccount": s,
                              "Status": "ACTIVE"} for i, c, n, t, s in ACCOUNTS]}
    if path == "Reports/ProfitAndLoss":
        return profit_and_loss(params)
    if path == "Reports/BalanceSheet":
        return balance_sheet(params)
    if path == "Reports/TrialBalance":
        return {"Reports": [{"Rows": [
            {"RowType": "Header", "Cells": [_cell("Account"), _cell("Debit"), _cell("Credit"), _cell("YTD Debit"),
                                            _cell("YTD Credit")]},
            _section("Revenue", [_row("Sales (200)", "a-sales", "", "10000.00", "", "30000.00")]),
        ]}]}
    if path == "Budgets":
        return {"Budgets": [{"BudgetID": "b-track", "Type": "TRACKING"}, {"BudgetID": "b-1", "Type": "OVERALL"}]}
    if path == "Budgets/b-1":
        return {"Budgets": [{"BudgetLines": [
            {"AccountID": "a-sales", "BudgetBalances": [
                {"Period": "2026-07", "Amount": "11000"}, {"Period": "2026-08", "Amount": "11500"},
                {"Period": "2026-09", "Amount": "12000"}, {"Period": "2026-10", "Amount": "12400"}]},
            {"AccountID": "a-wages", "BudgetBalances": [{"Period": "2026-10", "Amount": "3100"}]},
            {"AccountID": "a-bank", "BudgetBalances": [{"Period": "2026-10", "Amount": "999"}]},
        ]}]}
    if path == "Invoices":
        return invoices(params)
    if path == "BankTransactions":
        return {"BankTransactions": [
            {"Type": "SPEND", "BankAccount": {"AccountID": "a-bank"}, "DateString": "2026-09-28T00:00:00", "Total": "120.00"},
            {"Type": "RECEIVE", "BankAccount": {"AccountID": "a-bank"}, "Date": "/Date(1790726400000+0000)/", "Total": "500.00"},
        ]}
    if path == "Payments":
        return {"Payments": [{"PaymentType": "ACCPAYPAYMENT", "Account": {"AccountID": "a-bank"},
                              "Date": "/Date(1789776000000+0000)/", "Amount": "80.00"}]}
    if path == "CreditNotes":
        where = params["where"]
        if where.startswith("CreditNoteNumber=="):
            if where != 'CreditNoteNumber=="OE-CN1"':
                return {"CreditNotes": []}
            return {"CreditNotes": [{
                "CreditNoteID": "cn-1", "CreditNoteNumber": "OE-CN1", "Type": "ACCPAYCREDIT", "Status": "PAID",
                "Contact": {"Name": "Origin Energy"}, "DateString": "2026-08-20T00:00:00", "CurrencyCode": "AUD",
                "SubTotal": "100.00", "TotalTax": "10.00", "Total": "110.00", "RemainingCredit": "30.00",
                "LineItems": [{"Description": "Billing error refund", "AccountCode": "445", "Quantity": "1.0000",
                               "UnitAmount": "100.00", "LineAmount": "100.00"}],
            }]}
        rows = CREDIT_HISTORY if 'Type=="ACCPAYCREDIT"' in where else []
        return {"CreditNotes": _history(rows, where, "CreditNoteNumber", "ACCPAYCREDIT")}
    if path == "Contacts":
        contacts = [{"ContactID": "c-smith", "Name": "Smith Wedding", "EmailAddress": "smith@example.com",
                     "IsCustomer": True, "ContactStatus": "ACTIVE"},
                    {"ContactID": "c-harbour", "Name": "Harbour Events Pty Ltd", "EmailAddress": "accounts@harbour.test",
                     "IsCustomer": True, "ContactStatus": "ACTIVE"}]
        term = params.get("searchTerm", "").lower()
        return {"Contacts": [c for c in contacts if term in c["Name"].lower()] if term != "smith" else contacts[:1]}
    if path == "TaxRates":
        return {"TaxRates": [
            {"Name": "GST on Income", "TaxType": "OUTPUT", "EffectiveRate": "10.0000", "Status": "ACTIVE"},
            {"Name": "GST Free Income", "TaxType": "EXEMPTOUTPUT", "EffectiveRate": "0.0000", "Status": "ACTIVE"},
            {"Name": "Old Rate", "TaxType": "OLD", "EffectiveRate": "5.0000", "Status": "DELETED"}]}
    if path == "TrackingCategories":
        return {"TrackingCategories": [{"Name": "Event", "Status": "ACTIVE", "Options": [
            {"Name": "Celtic Nights", "Status": "ACTIVE"}, {"Name": "Gala Dinner", "Status": "ACTIVE"},
            {"Name": "Old Show", "Status": "ARCHIVED"}]}]}
    raise AssertionError(f"unexpected call {path} {params}")


class FakeClient(XeroClient):
    def __init__(self, scopes=ALL_SCOPES):
        super().__init__(SimpleNamespace(scopes=scopes, tenant_id=f"fake-{uuid.uuid4()}"))
        self.calls = []

    def _request(self, path, params, *, retried=False):
        self.calls.append((path, dict(params)))
        return route(path, params)


def fake_source(scopes=ALL_SCOPES):
    source = XeroSource(connection=None)
    source.client = FakeClient(scopes)
    return source
