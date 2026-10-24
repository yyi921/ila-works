"""Finance figures for console commands, read live from Xero. Commands use only this interface (no Xero names), so
another source system can supply the same methods later.

P&L categories come from account types, not report section titles, which vary by organisation:
revenue = Revenue and Sales accounts; other income = Other Income; cost of sales = Direct Costs;
expenses = Expense, Overhead and Depreciation accounts. Net profit is taken from Xero's own Net Profit row, and
anything not attributable to an account (e.g. currency gains) is shown separately, so totals tie to Xero.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .api import XeroAPIError, XeroClient, XeroScopeMissing, record_date, report_rows, to_decimal

ZERO = Decimal("0")
CATEGORIES = ("revenue", "other_income", "cost_of_sales", "expenses")
CATEGORY_OF_TYPE = {
    "REVENUE": "revenue", "SALES": "revenue", "OTHERINCOME": "other_income", "DIRECTCOSTS": "cost_of_sales",
    "EXPENSE": "expenses", "OVERHEADS": "expenses", "DEPRECIATN": "expenses",
}
INCOME_CATEGORIES = ("revenue", "other_income")


@dataclass
class ProfitAndLoss:
    """Amounts by account id, plus Xero's net profit. Income and costs are both positive, as Xero shows them."""

    by_account: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    net_profit: Decimal = ZERO
    category_of: dict = field(default_factory=dict)

    def total(self, category):
        return sum((v for a, v in self.by_account.items() if self.category_of.get(a) == category), ZERO)

    @property
    def gross_profit(self):
        return self.total("revenue") - self.total("cost_of_sales")

    @property
    def unattributed(self):
        """Net profit not explained by account rows (shown as its own line so totals tie to Xero)."""
        explained = sum(self.total(c) for c in INCOME_CATEGORIES) - self.total("cost_of_sales") - self.total("expenses")
        return self.net_profit - explained

    def measure(self, name):
        if name == "gross_profit":
            return self.gross_profit
        if name == "net_profit":
            return self.net_profit
        if name == "expenses":
            return self.total("expenses") - self.unattributed
        return self.total(name)


def _is_whole_month(period):
    last = calendar.monthrange(period.start.year, period.start.month)[1]
    return period.start.day == 1 and period.end == date(period.start.year, period.start.month, last)


# Documents that count in the P&L: approved or paid, not draft, awaiting approval, voided or deleted.
_LIVE = 'Status!="DRAFT" AND Status!="SUBMITTED" AND Status!="VOIDED" AND Status!="DELETED"'


def _date_filter(start, end):
    return (f"Date>=DateTime({start.year},{start.month},{start.day}) AND "
            f"Date<=DateTime({end.year},{end.month},{end.day})")


def _overlap_days(start, end, month_start):
    month_end = date(month_start.year, month_start.month, calendar.monthrange(month_start.year, month_start.month)[1])
    lo, hi = max(start, month_start), min(end, month_end)
    return max((hi - lo).days + 1, 0), (month_end - month_start).days + 1


class XeroSource:
    name = "Xero"

    def __init__(self, connection):
        self.connection = connection
        self.client = XeroClient(connection)

    # --- Settings ---

    def settings(self):
        org = self.client.get("Organisation", cache_seconds=3600)["Organisations"][0]
        return {
            "name": org.get("Name", ""), "base_currency": org.get("BaseCurrency", ""),
            "fy_end_month": int(org.get("FinancialYearEndMonth") or 6),
            "fy_end_day": int(org.get("FinancialYearEndDay") or 30),
        }

    def accounts(self):
        """Chart of accounts: id -> dict(code, name, type, category, system, is_bank, currency)."""
        result = {}
        for a in self.client.get("Accounts", cache_seconds=3600).get("Accounts", []):
            result[a["AccountID"]] = {
                "code": a.get("Code", ""), "name": a.get("Name", ""), "type": a.get("Type", ""),
                "category": CATEGORY_OF_TYPE.get(a.get("Type", "")), "system": a.get("SystemAccount", ""),
                "is_bank": a.get("Type") == "BANK", "currency": a.get("CurrencyCode", ""),
                "status": a.get("Status", ""),
            }
        return result

    # --- Profit and loss ---

    def profit_and_loss(self, start, end, *, with_last_year=False):
        """ProfitAndLoss for the period; with ``with_last_year`` a pair (this year, same dates last year) from one
        call, using Xero's comparative column."""
        params = {"fromDate": start.isoformat(), "toDate": end.isoformat(), "standardLayout": "true"}
        if with_last_year:
            params |= {"periods": 1, "timeframe": "YEAR"}
        results = self._pl_columns(params, 2 if with_last_year else 1)
        return tuple(results) if with_last_year else results[0]

    def profit_and_loss_by_month(self, months):
        """ProfitAndLoss for each of ``months`` (consecutive periods, oldest first; the first and last may be part
        months). Whole months come from Xero's monthly comparative columns, up to 12 per call, newest first."""
        results = {}
        whole = [m for m in months if _is_whole_month(m)]
        for i in range(len(whole), 0, -12):
            chunk = whole[max(i - 12, 0):i]
            base = chunk[-1]
            params = {"fromDate": base.start.isoformat(), "toDate": base.end.isoformat(), "standardLayout": "true"}
            if len(chunk) > 1:
                params |= {"periods": len(chunk) - 1, "timeframe": "MONTH"}
            for k, pl in enumerate(self._pl_columns(params, len(chunk))):
                results[chunk[-1 - k]] = pl
        for m in months:
            if m not in results:
                results[m] = self.profit_and_loss(m.start, m.end)
        return [results[m] for m in months]

    def _pl_columns(self, params, columns):
        header, rows = report_rows(self.client.get("Reports/ProfitAndLoss", params))
        accounts = self.accounts()
        results = [ProfitAndLoss() for _ in range(columns)]
        for row in rows:
            if row["type"] == "SummaryRow":
                continue
            label = row["label"].strip().lower()
            for i, pl in enumerate(results):
                value = to_decimal(row["values"][i]) if i < len(row["values"]) else ZERO
                if label in ("net profit", "net loss") and not row["account_id"]:
                    pl.net_profit = value
                elif row["account_id"]:
                    pl.by_account[row["account_id"]] = pl.by_account.get(row["account_id"], ZERO) + value
                    pl.names[row["account_id"]] = row["label"]
        for pl in results:
            pl.category_of = {a: accounts.get(a, {}).get("category") for a in pl.by_account}
        return results

    # --- Budget ---

    def overall_budget_id(self):
        self.client.require_scope("accounting.budgets.read")
        for budget in self.client.get("Budgets", cache_seconds=3600).get("Budgets", []):
            if budget.get("Type") == "OVERALL":
                return budget["BudgetID"]
        return None

    def budget(self, start, end):
        """ProfitAndLoss of the overall budget for the period, months pro-rated by calendar days for partial
        months. None if the organisation has no overall budget or iLA lacks the budgets permission."""
        if not self.client.has_scope("accounting.budgets.read"):
            return None
        budget_id = self.overall_budget_id()
        if not budget_id:
            return None
        first, last = date(start.year, start.month, 1), date(end.year, end.month, 1)
        data = self.client.get(f"Budgets/{budget_id}", {
            "DateFrom": first.isoformat(),
            "DateTo": date(end.year, end.month, calendar.monthrange(end.year, end.month)[1]).isoformat(),
        })
        accounts = self.accounts()
        pl = ProfitAndLoss()
        for line in (data.get("Budgets") or [{}])[0].get("BudgetLines", []):
            account_id = line.get("AccountID")
            if accounts.get(account_id, {}).get("category") is None:
                continue
            amount = ZERO
            for bal in line.get("BudgetBalances", []):
                year, month = (int(p) for p in bal["Period"][:7].split("-"))
                month_start = date(year, month, 1)
                if first <= month_start <= last:
                    days, days_in_month = _overlap_days(start, end, month_start)
                    amount += to_decimal(bal.get("Amount")) * days / days_in_month
            pl.by_account[account_id] = amount.quantize(Decimal("0.01"))
            pl.names[account_id] = accounts[account_id]["name"]
        pl.category_of = {a: accounts[a]["category"] for a in pl.by_account}
        pl.net_profit = sum((pl.total(c) for c in INCOME_CATEGORIES), ZERO) - pl.total("cost_of_sales") - pl.total("expenses")
        return pl

    # --- Balance sheet, trial balance ---

    def balance_sheet(self, as_at):
        """Rows of the balance sheet as at a date: dicts (section, label, account_id, amount, is_total)."""
        self.client.require_scope("accounting.reports.balancesheet.read")
        _, rows = report_rows(self.client.get("Reports/BalanceSheet",
                                              {"date": as_at.isoformat(), "standardLayout": "true"}))
        return [{"section": r["section"], "label": r["label"], "account_id": r["account_id"],
                 "amount": to_decimal(r["values"][0] if r["values"] else ""), "is_total": r["type"] == "SummaryRow"}
                for r in rows]

    def bank_balances(self, as_at):
        accounts = self.accounts()
        return [{"account": accounts[r["account_id"]]["name"], "account_id": r["account_id"], "balance": r["amount"]}
                for r in self.balance_sheet(as_at) if r["account_id"] and accounts.get(r["account_id"], {}).get("is_bank")]

    def system_balance(self, as_at, system_account):
        """Balance of a system account (DEBTORS, CREDITORS) as at a date."""
        ids = {a for a, acc in self.accounts().items() if acc["system"] == system_account}
        return sum((r["amount"] for r in self.balance_sheet(as_at) if r["account_id"] in ids), ZERO)

    def trial_balance(self, as_at):
        header, rows = report_rows(self.client.get("Reports/TrialBalance", {"date": as_at.isoformat()}))
        return header[1:], [{"section": r["section"], "label": r["label"], "account_id": r["account_id"],
                             "values": [to_decimal(v) for v in r["values"]], "is_total": r["type"] == "SummaryRow"}
                            for r in rows]

    # --- Receivables and payables ---

    def open_documents(self, ledger, *, due_by=None):
        """Authorised invoices (ledger "receivable") or bills ("payable") with an amount due, optionally only those
        due on or before ``due_by``. Amounts in base currency at the document's rate."""
        kind = "ACCREC" if ledger == "receivable" else "ACCPAY"
        where = f'Type=="{kind}" AND Status=="AUTHORISED" AND AmountDue>0'
        if due_by:
            where += f" AND DueDate<=DateTime({due_by.year},{due_by.month},{due_by.day})"
        docs = []
        for inv in self.client.get_all("Invoices", "Invoices", {"where": where, "summaryOnly": "true"}):
            rate = to_decimal(inv.get("CurrencyRate")) or Decimal("1")
            due = to_decimal(inv.get("AmountDue"))
            docs.append({
                "number": inv.get("InvoiceNumber") or inv.get("Reference") or "", "contact": (inv.get("Contact") or {}).get("Name", ""),
                "date": record_date(inv), "due_date": record_date(inv, "DueDate"), "currency": inv.get("CurrencyCode", ""),
                "amount_due": due, "amount_due_base": (due / rate).quantize(Decimal("0.01")),
                "id": inv.get("InvoiceID"),
            })
        return docs

    def documents(self, ledger, start, end):
        """Invoices (ledger "receivable") or bills ("payable") and their credit notes dated in the period, as they
        count in the P&L: approved or paid only. Lines are ex tax in base currency at each document's rate; credit
        notes are negative."""
        kind = "ACCREC" if ledger == "receivable" else "ACCPAY"
        where = f'{_date_filter(start, end)} AND {_LIVE}'
        docs = [self._document_row(d, "InvoiceNumber", 1)
                for d in self.client.get_all("Invoices", "Invoices", {"where": f'Type=="{kind}" AND {where}'})]
        try:
            credits = self.client.get_all("CreditNotes", "CreditNotes", {"where": f'Type=="{kind}CREDIT" AND {where}'})
        except XeroScopeMissing:
            self.credit_notes_unavailable = True
            credits = []
        docs += [self._document_row(d, "CreditNoteNumber", -1) for d in credits]
        return docs

    @staticmethod
    def _document_row(doc, number_field, sign):
        rate = to_decimal(doc.get("CurrencyRate")) or Decimal("1")
        inclusive = doc.get("LineAmountTypes") == "Inclusive"
        contact = doc.get("Contact") or {}
        lines = []
        for li in doc.get("LineItems") or []:
            amount = to_decimal(li.get("LineAmount")) - (to_decimal(li.get("TaxAmount")) if inclusive else ZERO)
            lines.append({"account": li.get("AccountCode", ""), "description": li.get("Description", ""),
                          "amount": (sign * amount / rate).quantize(Decimal("0.01"))})
        if not lines:  # no line detail returned: fall back to the ex-tax subtotal
            lines.append({"account": "", "description": "",
                          "amount": (sign * to_decimal(doc.get("SubTotal")) / rate).quantize(Decimal("0.01"))})
        return {
            "number": doc.get(number_field) or doc.get("Reference") or "",
            "type": "Credit note" if sign < 0 else ("Invoice" if doc.get("Type") == "ACCREC" else "Bill"),
            "contact": contact.get("Name", ""),
            "contact_codes": {c.lower() for c in (contact.get("AccountNumber"), contact.get("ContactNumber")) if c},
            "date": record_date(doc), "currency": doc.get("CurrencyCode", ""), "lines": lines,
            "amount": sum((li["amount"] for li in lines), ZERO),
        }

    def document(self, number):
        data = self.client.get("Invoices", {"InvoiceNumbers": number})
        docs = data.get("Invoices", [])
        if not docs:
            return self.credit_note(number)
        inv = docs[0]
        if not inv.get("LineItems"):
            inv = self.client.get(f"Invoices/{inv['InvoiceID']}")["Invoices"][0]
        return {
            "number": inv.get("InvoiceNumber", ""), "type": "Invoice" if inv.get("Type") == "ACCREC" else "Bill",
            "contact": (inv.get("Contact") or {}).get("Name", ""), "status": inv.get("Status", "").title(),
            "date": record_date(inv), "due_date": record_date(inv, "DueDate"), "reference": inv.get("Reference", ""),
            "currency": inv.get("CurrencyCode", ""), "subtotal": to_decimal(inv.get("SubTotal")),
            "tax": to_decimal(inv.get("TotalTax")), "total": to_decimal(inv.get("Total")),
            "paid": to_decimal(inv.get("AmountPaid")), "due": to_decimal(inv.get("AmountDue")),
            "lines": [{"description": li.get("Description", ""), "account": li.get("AccountCode", ""),
                       "quantity": to_decimal(li.get("Quantity")), "unit": to_decimal(li.get("UnitAmount")),
                       "amount": to_decimal(li.get("LineAmount"))} for li in inv.get("LineItems", [])],
        }

    def credit_note(self, number):
        """A sales or supplier credit note by number, in the same shape as ``document``: "paid" is the credit
        already allocated or refunded and "due" the credit remaining."""
        safe = number.replace('"', "")
        notes = self.client.get("CreditNotes", {"where": f'CreditNoteNumber=="{safe}"'}).get("CreditNotes", [])
        if not notes:
            return None
        note = notes[0]
        if not note.get("LineItems"):
            note = self.client.get(f"CreditNotes/{note['CreditNoteID']}")["CreditNotes"][0]
        total, remaining = to_decimal(note.get("Total")), to_decimal(note.get("RemainingCredit"))
        return {
            "number": note.get("CreditNoteNumber", ""), "is_credit": True,
            "type": "Credit note" if note.get("Type") == "ACCRECCREDIT" else "Supplier credit note",
            "contact": (note.get("Contact") or {}).get("Name", ""), "status": note.get("Status", "").title(),
            "date": record_date(note), "due_date": None, "reference": note.get("Reference", ""),
            "currency": note.get("CurrencyCode", ""), "subtotal": to_decimal(note.get("SubTotal")),
            "tax": to_decimal(note.get("TotalTax")), "total": total, "paid": total - remaining, "due": remaining,
            "lines": [{"description": li.get("Description", ""), "account": li.get("AccountCode", ""),
                       "quantity": to_decimal(li.get("Quantity")), "unit": to_decimal(li.get("UnitAmount")),
                       "amount": to_decimal(li.get("LineAmount"))} for li in note.get("LineItems", [])],
        }

    def find(self, text):
        """Contacts matching the text, and invoices/bills whose number or reference contains it."""
        safe = text.replace('"', "")
        contacts = self.client.get("Contacts", {"searchTerm": safe, "summaryOnly": "true", "page": 1}).get("Contacts", [])
        invoices = self.client.get("Invoices", {
            "where": f'InvoiceNumber.Contains("{safe}") OR Reference.Contains("{safe}")', "summaryOnly": "true", "page": 1,
        }).get("Invoices", [])
        return (
            [{"name": c.get("Name", ""), "email": c.get("EmailAddress", ""),
              "customer": bool(c.get("IsCustomer")), "supplier": bool(c.get("IsSupplier"))} for c in contacts[:50]],
            [{"number": i.get("InvoiceNumber", ""), "type": "Invoice" if i.get("Type") == "ACCREC" else "Bill",
              "contact": (i.get("Contact") or {}).get("Name", ""), "date": record_date(i),
              "status": i.get("Status", "").title(), "total": to_decimal(i.get("Total")),
              "due": to_decimal(i.get("AmountDue"))} for i in invoices[:50]],
        )

    # --- Invoice creation (read-only look-ups for /_createInvoice) ---

    def customers(self, text):
        """Contacts whose name, email or account number contains the text (Xero's search), best first."""
        safe = text.replace('"', "")
        contacts = self.client.get("Contacts", {"searchTerm": safe, "summaryOnly": "true", "page": 1}).get("Contacts", [])
        return [{"id": c.get("ContactID", ""), "name": c.get("Name", ""), "email": c.get("EmailAddress", ""),
                 "account_number": c.get("AccountNumber", ""), "status": c.get("ContactStatus", "")}
                for c in contacts if c.get("ContactStatus", "ACTIVE") == "ACTIVE"][:50]

    def tax_rates(self):
        """Tax rates by name: {name: {"type": code, "rate": Decimal percent, "active": bool}}."""
        return {t.get("Name", ""): {"type": t.get("TaxType", ""), "rate": to_decimal(t.get("EffectiveRate")),
                                    "active": t.get("Status") == "ACTIVE"}
                for t in self.client.get("TaxRates", cache_seconds=3600).get("TaxRates", [])}

    # --- Raw transactions, for copying into iLA (xero.sync) ---

    SYNC_CACHE_SECONDS = 60

    def _period_pages(self, path, collection, start, end):
        """Every record of ``path`` dated ``start`` to ``end`` (both included). Xero returns 100 per page, line
        items included, so a short page is the last."""
        where = (f"Date>=DateTime({start.year},{start.month:02d},{start.day:02d}) AND "
                 f"Date<=DateTime({end.year},{end.month:02d},{end.day:02d})")
        rows, page = [], 1
        while True:
            batch = self.client.get(path, {"where": where, "page": page},
                                    cache_seconds=self.SYNC_CACHE_SECONDS).get(collection, [])
            rows += batch
            if len(batch) < 100:
                return rows
            page += 1

    def raw_invoices(self, start, end):
        """Sales invoices and bills (Xero's Invoices, types ACCREC and ACCPAY) in the period, any status."""
        return self._period_pages("Invoices", "Invoices", start, end)

    def raw_credit_notes(self, start, end):
        return self._period_pages("CreditNotes", "CreditNotes", start, end)

    def raw_bank_transactions(self, start, end):
        return self._period_pages("BankTransactions", "BankTransactions", start, end)

    def raw_manual_journals(self, start, end):
        return self._period_pages("ManualJournals", "ManualJournals", start, end)

    def raw_accounts(self, *, fresh=False):
        return self.client.get("Accounts", cache_seconds=0 if fresh else 3600).get("Accounts", [])

    def tracking_categories(self):
        """Active tracking categories: {name: [active option names]}."""
        return {c.get("Name", ""): [o.get("Name", "") for o in c.get("Options", []) if o.get("Status") == "ACTIVE"]
                for c in self.client.get("TrackingCategories", cache_seconds=3600).get("TrackingCategories", [])
                if c.get("Status") == "ACTIVE"}

    def tracking_category_list(self):
        """Every tracking category, archived ones included, with all its options and their status:
        [{"id", "name", "status", "options": [{"id", "name", "status"}]}]."""
        data = self.client.get("TrackingCategories", {"includeArchived": "true"}, cache_seconds=300)
        return [{"id": c.get("TrackingCategoryID", ""), "name": c.get("Name", ""),
                 "status": c.get("Status", "").title(),
                 "options": [{"id": o.get("TrackingOptionID", ""), "name": o.get("Name", ""),
                              "status": o.get("Status", "").title()}
                             for o in c.get("Options", [])]}
                for c in data.get("TrackingCategories", [])]

    def recent_sales_invoices(self):
        """The 100 most recent sales invoices in any live or draft status, with their lines, so a new invoice can
        follow how similar lines were coded before. Tax types are Xero codes (e.g. OUTPUT)."""
        where = 'Type=="ACCREC" AND Status!="DELETED" AND Status!="VOIDED"'
        invoices = self.client.get("Invoices", {"where": where, "order": "Date DESC", "page": 1}).get("Invoices", [])
        return [{
            "number": i.get("InvoiceNumber", ""), "contact": (i.get("Contact") or {}).get("Name", ""),
            "contact_id": (i.get("Contact") or {}).get("ContactID", ""), "reference": i.get("Reference", ""),
            "date": record_date(i), "status": i.get("Status", "").title(),
            "tax_basis": i.get("LineAmountTypes", ""),
            "lines": [{"description": li.get("Description", ""), "account": li.get("AccountCode", ""),
                       "tax_type": li.get("TaxType", ""), "unit": to_decimal(li.get("UnitAmount")),
                       "tracking": [(t.get("Name", ""), t.get("Option", "")) for t in li.get("Tracking") or []]}
                      for li in i.get("LineItems") or []],
        } for i in invoices]

    def invoice_number_used(self, number):
        """Whether any invoice or bill already has this number, in any status."""
        return bool(self.client.get("Invoices", {"InvoiceNumbers": number, "summaryOnly": "true"},
                                    cache_seconds=0).get("Invoices"))

    def ledger_balance(self, ledger, as_at):
        """Debtors (ledger "receivable") or creditors ("payable") at the end of ``as_at``, in base currency at each
        document's rate: {"total", "documents"}. Xero only gives what is owed today, so this works back from it:
        what's open today on documents dated by ``as_at``, plus what was paid or allocated to them after it, less
        the credit notes, overpayments and prepayments that were open at ``as_at``."""
        kind, credit_kind = ("ACCREC", "ACCRECCREDIT") if ledger == "receivable" else ("ACCPAY", "ACCPAYCREDIT")
        by = f"DateTime({as_at.year},{as_at.month},{as_at.day})"

        def rate(doc):
            return to_decimal(doc.get("CurrencyRate")) or Decimal("1")

        def dated_by(doc):
            day = record_date(doc)
            return day is not None and day <= as_at

        total, count = ZERO, 0
        for inv in self.client.get_all("Invoices", "Invoices", {
                "where": f'Type=="{kind}" AND Status=="AUTHORISED" AND Date<={by}', "summaryOnly": "true"}):
            total += to_decimal(inv.get("AmountDue")) / rate(inv)
            count += 1
        credits = [c for c in self.client.get_all("CreditNotes", "CreditNotes", {"where": f'Type=="{credit_kind}"'})
                   if c.get("Status") in ("AUTHORISED", "PAID")]
        pre_over = []
        for path in ("Overpayments", "Prepayments"):
            prefix = "RECEIVE" if ledger == "receivable" else "SPEND"
            pre_over += [p for p in self.client.get_all(path, path)
                         if p.get("Type", "").startswith(prefix) and p.get("Status") in ("AUTHORISED", "PAID")]
        for credit in credits + pre_over:  # credit open at as_at: open today, plus what was used after as_at
            if dated_by(credit):
                total -= to_decimal(credit.get("RemainingCredit")) / rate(credit)
                count += 1
        after = [p for p in self.client.get_all("Payments", "Payments", {"where": f"Date>{by}"})
                 if p.get("Status") != "DELETED"]
        invoice_ids = {(p.get("Invoice") or {}).get("InvoiceID") for p in after}
        for credit in credits + pre_over:
            for alloc in credit.get("Allocations") or []:
                invoice_ids.add((alloc.get("Invoice") or {}).get("InvoiceID"))
        invoices = self._invoices_by_id(invoice_ids - {None})
        credit_by_id = {c.get("CreditNoteID") or c.get("OverpaymentID") or c.get("PrepaymentID"): c
                        for c in credits + pre_over}
        for pay in after:
            invoice = invoices.get((pay.get("Invoice") or {}).get("InvoiceID"))
            if invoice is not None and invoice.get("Type") == kind and dated_by(invoice):
                total += to_decimal(pay.get("Amount")) / rate(invoice)
            for key, id_field in (("CreditNote", "CreditNoteID"), ("Overpayment", "OverpaymentID"),
                                  ("Prepayment", "PrepaymentID")):
                credit = credit_by_id.get((pay.get(key) or {}).get(id_field))
                if credit is not None and dated_by(credit):  # a refund after as_at: the credit was still open
                    total -= to_decimal(pay.get("Amount")) / rate(credit)
        for credit in credits + pre_over:
            for alloc in credit.get("Allocations") or []:
                invoice = invoices.get((alloc.get("Invoice") or {}).get("InvoiceID"))
                # Xero's ledger takes an allocation on the latest of its own date and the two documents' dates
                # (an allocation can be dated before the credit note it uses).
                days = [d for d in (record_date(alloc), record_date(credit),
                                    record_date(invoice) if invoice else None) if d]
                if not days or max(days) <= as_at:
                    continue
                amount = to_decimal(alloc.get("Amount"))
                if invoice is not None and dated_by(invoice):
                    total += amount / rate(invoice)
                if dated_by(credit):
                    total -= amount / rate(credit)
        return {"total": total.quantize(Decimal("0.01")), "documents": count}

    def _invoices_by_id(self, ids):
        found = {}
        ids = sorted(ids)
        for start in range(0, len(ids), 40):
            batch = self.client.get("Invoices", {"IDs": ",".join(ids[start:start + 40]), "summaryOnly": "true"})
            found.update({inv["InvoiceID"]: inv for inv in batch.get("Invoices", [])})
        return found

    # --- Bank reconciliation ---

    def unreconciled_items(self, as_at=None):
        """Authorised bank transactions and payments not yet reconciled, dated on or before ``as_at`` if given: dicts
        (account_id, date, description, amount), amounts signed as they move the bank balance (receipts positive).
        Bank statement lines that haven't been matched or coded at all aren't visible through Xero's standard API,
        and Xero doesn't say when an item was reconciled, so an item dated before ``as_at`` but reconciled since
        doesn't appear."""
        accounts = self.accounts()
        items = []

        def add(account_id, day, description, amount):
            if not account_id or not accounts.get(account_id, {}).get("is_bank"):
                return
            if as_at and day and day > as_at:
                return
            items.append({"account_id": account_id, "date": day, "description": description, "amount": amount})

        where = 'IsReconciled==false AND Status=="AUTHORISED"'
        for txn in self.client.get_all("BankTransactions", "BankTransactions", {"where": where}):
            sign = -1 if txn.get("Type", "").startswith("SPEND") else 1
            text = " · ".join(p for p in ((txn.get("Contact") or {}).get("Name", ""), txn.get("Reference", "")) if p)
            add((txn.get("BankAccount") or {}).get("AccountID"), record_date(txn),
                text or ("Spend money" if sign < 0 else "Receive money"), sign * to_decimal(txn.get("Total")))
        for pay in self.client.get_all("Payments", "Payments", {"where": where}):
            sign = -1 if pay.get("PaymentType", "").startswith("ACCPAY") or pay.get("PaymentType") == "ARCREDITPAYMENT" else 1
            invoice = pay.get("Invoice") or {}
            text = " · ".join(p for p in ((invoice.get("Contact") or {}).get("Name", ""), invoice.get("InvoiceNumber", ""),
                                          pay.get("Reference", "")) if p)
            add((pay.get("Account") or {}).get("AccountID"), record_date(pay), text or "Payment",
                sign * to_decimal(pay.get("Amount")))
        return items

    def unreconciled(self):
        """Per bank account: count, net amount and oldest date of authorised transactions and payments not yet
        reconciled (see ``unreconciled_items``), so this can understate what is left to reconcile."""
        accounts = self.accounts()
        summary = {}
        for item in self.unreconciled_items():
            s = summary.setdefault(item["account_id"], {"account": accounts[item["account_id"]]["name"], "count": 0,
                                                        "amount": ZERO, "oldest": None})
            s["count"] += 1
            s["amount"] += item["amount"]
            if item["date"] and (s["oldest"] is None or item["date"] < s["oldest"]):
                s["oldest"] = item["date"]
        for account_id, acc in accounts.items():
            if acc["is_bank"] and acc["status"] != "ARCHIVED":
                summary.setdefault(account_id, {"account": acc["name"], "count": 0, "amount": ZERO, "oldest": None})
        return list(summary.values())


def source_for(organisation):
    """The live data source for an organisation, or raise XeroAPIError if none is connected."""
    connection = getattr(organisation, "xero_connection", None)
    if connection is None:
        raise XeroAPIError(f"{organisation} isn't connected to an accounting system. Connect it with /connect.")
    if not connection.is_active:
        raise XeroAPIError("The Xero connection has been disconnected. Reconnect with /connect.")
    return XeroSource(connection)

