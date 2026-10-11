"""Build a synthetic sales invoice import file for a test import (e.g. into Xero's Demo Company) and register it
under Import files so it can be downloaded from the admin.

    python manage.py sample_invoice_file --organisation "Demo Company (AU)" --email you@example.com

The two invoices are designed so the result of the import answers open questions:
ILA-TEST-001 has a Discount of 50 (a percentage would give 4,950.00, an amount 9,850.00), a GST-free line and a
$0 line; ILA-TEST-002 is labour by day from timesheets. Invoice dates use day 13, so a month-first reading fails
loudly instead of silently swapping day and month.
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.invoicing import build_csv
from core.models import ImportFile, ImportFormat, Organisation

CONTACT = "iLA Test Events Pty Ltd"


def sample_invoices(email):
    common = {"contact_name": CONTACT, "email": email, "address_line1": "1 Test Street", "city": "Townsville",
              "region": "QLD", "postal_code": "4810", "country": "Australia",
              "date": date(2026, 10, 13), "due_date": date(2026, 10, 27)}
    gst, free = "GST on Income", "GST Free Income"
    return [
        {**common, "number": "ILA-TEST-001", "reference": "iLA test - Riverside Gala 30.10", "lines": [
            {"description": "Venue Hire - Arena", "quantity": 1, "unit_amount": Decimal("9900.00"),
             "discount": 50, "account_code": "200", "tax_type": gst},
            {"description": "Venue Setup / Dismantle", "quantity": 1, "unit_amount": Decimal("4850.00"),
             "account_code": "200", "tax_type": gst},
            {"description": "First Aid (2 attendants x 5 hours)", "quantity": 10, "unit_amount": Decimal("100.00"),
             "account_code": "200", "tax_type": free},
            {"description": "Catering - 2 Course Plated Lunch", "quantity": 120, "unit_amount": Decimal("0.00"),
             "account_code": "200", "tax_type": gst},
        ]},
        {**common, "number": "ILA-TEST-002", "reference": "iLA test - JOB2026/10 (GL - 072)", "lines": [
            {"description": "Day Labour on 01 October", "quantity": 8, "unit_amount": Decimal("80.00"),
             "account_code": "200", "tax_type": gst},
            {"description": "Day Labour on 02 October", "quantity": 4, "unit_amount": Decimal("80.00"),
             "account_code": "200", "tax_type": gst},
        ]},
    ]


class Command(BaseCommand):
    help = "Build a synthetic sales invoice import file and register it under Import files."

    def add_arguments(self, parser):
        parser.add_argument("--organisation", required=True, help="Name of the organisation to file it under.")
        parser.add_argument("--email", default="", help="EmailAddress for the test contact (one you can check).")
        parser.add_argument("--format", default="Xero sales invoice CSV")
        parser.add_argument("--out", default="", help="Also write the file here.")

    def handle(self, *args, **options):
        organisation = Organisation.objects.filter(name__iexact=options["organisation"]).first()
        if organisation is None:
            raise CommandError(f"No organisation called {options['organisation']}.")
        import_format = ImportFormat.objects.get(name=options["format"])
        invoices = sample_invoices(options["email"])
        content = build_csv(import_format, invoices)
        file_name = "iLA test invoices ILA-TEST-001 and 002.csv"
        record = ImportFile.objects.create(
            organisation=organisation, target_system=import_format.system, profile=import_format.name,
            file_name=file_name, row_count=sum(len(i["lines"]) for i in invoices), content=content,
            content_type="text/csv", validation={"sample": True})
        if options["out"]:
            with open(options["out"], "wb") as handle:
                handle.write(content)
        self.stdout.write(f"Registered {file_name} (import file {record.pk}) for {organisation.name}.")
