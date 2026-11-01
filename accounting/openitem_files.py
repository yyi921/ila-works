"""Reading the two files a person can give an open-item ledger.

**A seed file** breaks the opening balance down: one row per description. Either a workbook in the iLA balance sheet
reconciliation layout (sheets Categories, Descriptions and Balance_Schedule: the open descriptions of the account
are read from Balance_Schedule, their categories from Categories), or one table (CSV, or the first sheet of an Excel
file) with the columns Description and Amount, and optionally Category code, Category, Desc code, Reference,
Counterparty, Service period and Account. Amounts may be signed either way (debit positive, or the way the balance
sheet shows them); the reading that agrees with Xero's opening balance is used.

**A GL file** is the account's transactions, e.g. Xero's Account Transactions report exported to Excel or CSV:
columns Date, Debit and Credit (or Net / Amount), and optionally Source, Description, Reference, Contact and Account
Code. It is used instead of iLA's copy of Xero when copying from Xero would take too long.
"""

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_BYTES = 10 * 1024 * 1024


class FileProblem(ValueError):
    """Shown to the user as is."""


def _money(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(CENT)
    text = str(value).replace(",", "").replace("$", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.endswith("-"):
        negative, text = True, text[:-1]
    try:
        amount = Decimal(text).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None
    return -amount if negative else amount


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
                    "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheets(upload):
    """{sheet name: [row tuples]} from an .xlsx or .csv upload."""
    name = upload.name.lower()
    if upload.size > MAX_BYTES:
        raise FileProblem(f"{upload.name} is over 10 MB.")
    raw = upload.read()
    upload.seek(0)
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        except Exception as exc:  # noqa: BLE001 - any unreadable workbook
            raise FileProblem(f"{upload.name} couldn't be opened as an Excel workbook ({exc}).") from None
        return {ws.title: [tuple(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    if name.endswith(".csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        return {"csv": [tuple(r) for r in csv.reader(io.StringIO(text))]}
    raise FileProblem(f"{upload.name} isn't an Excel (.xlsx) or CSV file.")


def _table(rows, must):
    """(header index {name: column}, data rows) from the first row that has every name in ``must`` (any of the
    alternatives in a tuple)."""
    for n, row in enumerate(rows[:40]):
        names = {_text(c).lower(): i for i, c in enumerate(row) if _text(c)}
        if all(any(alt in names for alt in (m if isinstance(m, tuple) else (m,))) for m in must):
            return names, rows[n + 1:]
    return None, []


def _cell(row, index, *names):
    for name in names:
        i = index.get(name)
        if i is not None and i < len(row) and row[i] not in (None, ""):
            return row[i]
    return None


def _sha(raw_upload):
    data = raw_upload.read()
    raw_upload.seek(0)
    return hashlib.sha256(data).hexdigest()


# --- Seed -------------------------------------------------------------------------------------------------------

def read_seed(upload, account, opening_balance):
    """(categories [{code, name, scope}], rows [{code, category, description, amount, counterparty, reference,
    service_period}], notes). Amounts debit positive."""
    sheets = _sheets(upload)
    code = (account.code or "").lower()
    categories, rows, notes = [], [], []
    by_name = {k.lower(): v for k, v in sheets.items()}
    if "balance_schedule" in by_name:
        index, data = _table(by_name["balance_schedule"], ["desc code", "description", ("balance", "amount")])
        if index is None:
            raise FileProblem("The Balance_Schedule sheet needs Desc code, Description and Balance columns.")
        parties = {}
        if "descriptions" in by_name:
            d_index, d_rows = _table(by_name["descriptions"], ["desc code", "description"])
            for r in d_rows if d_index else []:
                key = _text(_cell(r, d_index, "desc code"))
                if key:
                    parties[key] = _text(_cell(r, d_index, "counterparty"))
        seen = set()
        for r in data:
            desc_code = _text(_cell(r, index, "desc code"))
            if not desc_code or desc_code in seen:
                continue
            gl = _text(_cell(r, index, "gl", "account")).lower()
            if gl and code and gl != code:
                continue
            status = _text(_cell(r, index, "status")).lower()
            if status and status != "open":
                continue
            amount = _money(_cell(r, index, "balance", "amount"))
            if amount is None:
                raise FileProblem(f"{desc_code} has no balance figure. If the file has formulas, open it in Excel "
                                  "and save it, so the values are stored, then upload it again.")
            if not amount:
                continue
            seen.add(desc_code)
            reference = _text(_cell(r, index, "invoice / bill ref", "reference", "ref"))
            rows.append({"code": desc_code, "category": _text(_cell(r, index, "cat code", "category code")),
                         "description": _text(_cell(r, index, "description"))[:255],
                         "amount": amount, "counterparty": "" if parties.get(desc_code) == "Not Given"
                         else parties.get(desc_code, ""),
                         "reference": "" if reference in ("Not Given", "Invoice") else reference[:255],
                         "service_period": _date(_cell(r, index, "service period"))})
        if "categories" in by_name:
            c_index, c_rows = _table(by_name["categories"], [("cat code", "category code"), "category"])
            for r in c_rows if c_index else []:
                cat = _text(_cell(r, c_index, "cat code", "category code"))
                if not cat:
                    continue
                valid = _text(_cell(r, c_index, "valid gls", "account")).lower()
                active = _text(_cell(r, c_index, "active")).lower()
                if (valid and code and code not in re.split(r"[,\s]+", valid)) or active in ("n", "no"):
                    continue
                categories.append({"code": cat[:12], "name": _text(_cell(r, c_index, "category"))[:100],
                                   "scope": _text(_cell(r, c_index, "scope / examples", "scope"))[:255]})
    else:
        first = next(iter(sheets.values()))
        index, data = _table(first, ["description", ("amount", "balance")])
        if index is None:
            raise FileProblem(f"{upload.name} needs Description and Amount columns (and optionally Category code, "
                              "Category, Desc code, Reference, Counterparty, Service period, Account).")
        names = {}
        for n, r in enumerate(data, 2):
            description = _text(_cell(r, index, "description"))
            amount = _money(_cell(r, index, "amount", "balance"))
            if not description and amount is None:
                continue
            if not description or amount is None:
                raise FileProblem(f"Row {n}: needs a description and an amount.")
            gl = _text(_cell(r, index, "account", "gl")).lower()
            if gl and code and gl != code:
                continue
            cat = _text(_cell(r, index, "category code", "cat code"))
            cat_name = _text(_cell(r, index, "category"))
            if cat_name and not cat:
                cat = re.sub(r"[^A-Za-z0-9]", "", cat_name)[:6].upper() or "CAT"
            if cat:
                names.setdefault(cat, cat_name or cat)
            reference = _text(_cell(r, index, "reference", "invoice / bill ref", "ref"))
            rows.append({"code": _text(_cell(r, index, "desc code", "code")), "category": cat,
                         "description": description[:255], "amount": amount,
                         "counterparty": _text(_cell(r, index, "counterparty", "supplier", "contact"))[:255],
                         "reference": reference[:255],
                         "service_period": _date(_cell(r, index, "service period", "to"))})
        categories = [{"code": c[:12], "name": n[:100], "scope": ""} for c, n in names.items()]
    if not rows:
        raise FileProblem(f"{upload.name} has no open lines for {account}.")
    total = sum((r["amount"] for r in rows), ZERO)
    if abs(-total - opening_balance) < abs(total - opening_balance):
        for r in rows:
            r["amount"] = -r["amount"]
        total = -total
        notes.append("Amounts were signed the way the balance sheet shows them; read as debit positive.")
    for r in rows:
        r["amount"] = str(r["amount"])
        r["service_period"] = r["service_period"].isoformat() if r["service_period"] else ""
    known = {c["code"].lower() for c in categories}
    for r in rows:
        if r["category"] and r["category"].lower() not in known:
            categories.append({"code": r["category"][:12], "name": r["category"], "scope": ""})
            known.add(r["category"].lower())
    source = {"file": upload.name[:200], "sha256": _sha(upload), "rows": len(rows), "total": str(total)}
    return categories, rows, notes, source


# --- GL ---------------------------------------------------------------------------------------------------------

SOURCE_KIND = {"manual journal": "Manual journal", "journal": "Manual journal", "payable invoice": "Bill",
               "bill": "Bill", "accounts payable invoice": "Bill", "payable credit note": "Supplier credit note",
               "receivable invoice": "Sales invoice", "accounts receivable invoice": "Sales invoice",
               "receivable credit note": "Customer credit note", "spend money": "Spend money",
               "receive money": "Receive money"}


def read_gl(upload, account):
    """(rows [{id, date, amount (debit positive), description, reference, source, contact}], source info)."""
    sheets = _sheets(upload)
    code, name = (account.code or "").lower(), account.name.lower()
    rows = []
    for sheet in sheets.values():
        index, data = _table(sheet, ["date", ("debit", "credit", "net", "amount")])
        if index is None:
            continue
        section, counts = "", {}
        for r in data:
            day = _date(_cell(r, index, "date"))
            if day is None:
                first = _text(r[0]) if r else ""
                if first and not first.lower().startswith(("total", "opening balance", "closing balance")):
                    section = first.lower()
                continue
            account_code = _text(_cell(r, index, "account code", "code")).lower()
            if account_code and code and account_code != code:
                continue
            if not account_code and section and code and code not in section and name not in section:
                continue
            description = _text(_cell(r, index, "description", "details"))
            if description.lower().startswith(("opening balance", "closing balance", "total")):
                continue
            debit, credit = _money(_cell(r, index, "debit")), _money(_cell(r, index, "credit"))
            if debit is None and credit is None:
                amount = _money(_cell(r, index, "net", "amount"))
            else:
                amount = (debit or ZERO) - (credit or ZERO)
            if not amount:
                continue
            source = _text(_cell(r, index, "source", "type"))
            reference = _text(_cell(r, index, "reference"))
            base = f"{day}|{source}|{description}|{reference}|{amount}"
            counts[base] = counts.get(base, 0) + 1
            rows.append({"id": "GL" + hashlib.sha256(f"{base}|{counts[base]}".encode()).hexdigest()[:14],
                         "date": day.isoformat(), "amount": str(amount), "description": description[:500],
                         "reference": reference[:255], "source": SOURCE_KIND.get(source.lower(), "GL row"),
                         "contact": _text(_cell(r, index, "contact", "name"))[:255]})
        if rows:
            break
    if not rows:
        raise FileProblem(f"{upload.name} has no transactions for {account}: it needs Date and Debit/Credit (or "
                          "Net) columns, e.g. Xero's Account Transactions report for the account.")
    days = [r["date"] for r in rows]
    return rows, {"file": upload.name[:200], "sha256": _sha(upload), "rows": len(rows), "first": min(days),
                  "last": max(days)}
