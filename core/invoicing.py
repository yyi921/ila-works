"""Write customer invoices as import files in a target system's format.

An invoice here is a plain dict (see ``build_csv``) so the same writer serves /_createInvoice, tests and one-off
files. The format's ``columns`` decide the column order and headers; each column's ``field`` names the invoice or
line value that fills it.
"""

import csv
import io
from decimal import Decimal

# Xero's sales invoice import template, as downloaded from two AU organisations on 11 October 2026 (identical).
XERO_SALES_INVOICE_COLUMNS = [
    {"name": "*ContactName", "field": "contact_name"},
    {"name": "EmailAddress", "field": "email"},
    {"name": "POAddressLine1", "field": "address_line1"},
    {"name": "POAddressLine2", "field": "address_line2"},
    {"name": "POAddressLine3", "field": "address_line3"},
    {"name": "POAddressLine4", "field": "address_line4"},
    {"name": "POCity", "field": "city"},
    {"name": "PORegion", "field": "region"},
    {"name": "POPostalCode", "field": "postal_code"},
    {"name": "POCountry", "field": "country"},
    {"name": "*InvoiceNumber", "field": "number"},
    {"name": "Reference", "field": "reference"},
    {"name": "*InvoiceDate", "field": "date"},
    {"name": "*DueDate", "field": "due_date"},
    {"name": "InventoryItemCode", "field": "line.item_code"},
    {"name": "*Description", "field": "line.description"},
    {"name": "*Quantity", "field": "line.quantity"},
    {"name": "*UnitAmount", "field": "line.unit_amount"},
    {"name": "Discount", "field": "line.discount"},
    {"name": "*AccountCode", "field": "line.account_code"},
    {"name": "*TaxType", "field": "line.tax_type"},
    {"name": "TrackingName1", "field": "line.tracking_name1"},
    {"name": "TrackingOption1", "field": "line.tracking_option1"},
    {"name": "TrackingName2", "field": "line.tracking_name2"},
    {"name": "TrackingOption2", "field": "line.tracking_option2"},
    {"name": "Currency", "field": "currency"},
    {"name": "BrandingTheme", "field": "branding_theme"},
]

XERO_SALES_INVOICE_NOTES = (
    "One row per invoice line; rows with the same InvoiceNumber become one invoice. Imported invoices are drafts. "
    "Dates are read in the organisation's regional format (no choice on the import screen). Tax rate names must "
    "match the organisation's tax rates exactly. A ContactName that doesn't match an existing contact may create a "
    "new contact. About 500 invoices per file at most."
)


class InvoiceFileError(ValueError):
    pass


def _value(value, date_format):
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime(date_format)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def build_csv(import_format, invoices, *, date_format="%d/%m/%Y"):
    """CSV bytes for ``invoices`` in ``import_format``.

    Each invoice is a dict of invoice fields (``contact_name``, ``number``, ``date``…) with ``lines``: a list of
    dicts of line fields (``description``, ``quantity``, ``unit_amount``…). Required columns left empty are
    errors, listed together, so a file never fails on import for a missing value.
    """
    problems = []
    rows = []
    for invoice in invoices:
        lines = invoice.get("lines") or []
        if not lines:
            problems.append(f"{invoice.get('number') or 'An invoice'} has no lines.")
        for n, line in enumerate(lines, 1):
            row = []
            for column in import_format.columns:
                field = column["field"]
                value = line.get(field[5:]) if field.startswith("line.") else invoice.get(field)
                text = _value(value, date_format)
                if column["name"].startswith("*") and text == "":
                    problems.append(f"{invoice.get('number') or 'An invoice'} line {n}: "
                                    f"{column['name'].lstrip('*')} is required.")
                row.append(text)
            rows.append(row)
    if problems:
        raise InvoiceFileError(" ".join(problems))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(import_format.headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
