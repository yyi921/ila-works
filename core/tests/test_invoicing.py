import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command

from core.invoicing import InvoiceFileError, build_csv
from core.models import AuditLog, ImportFile, ImportFormat, InvoiceItem, InvoiceSetup, Organisation

TEMPLATE_HEADER = ("*ContactName,EmailAddress,POAddressLine1,POAddressLine2,POAddressLine3,POAddressLine4,POCity,"
                   "PORegion,POPostalCode,POCountry,*InvoiceNumber,Reference,*InvoiceDate,*DueDate,InventoryItemCode,"
                   "*Description,*Quantity,*UnitAmount,Discount,*AccountCode,*TaxType,TrackingName1,TrackingOption1,"
                   "TrackingName2,TrackingOption2,Currency,BrandingTheme")


@pytest.fixture
def xero_format(db):
    return ImportFormat.objects.get(name="Xero sales invoice CSV")


def invoice(**extra):
    return {"contact_name": "Base Events", "number": "INV-9001", "reference": "Gala 30.10",
            "date": date(2026, 10, 13), "due_date": date(2026, 10, 27), **extra,
            "lines": extra["lines"] if "lines" in extra else [
                {"description": "Venue Hire", "quantity": 1, "unit_amount": Decimal("4400.00"),
                 "account_code": "200", "tax_type": "GST on Income", "tracking_name1": "Event",
                 "tracking_option1": "Gala"},
                {"description": "Security", "quantity": 21, "unit_amount": Decimal("85.00"),
                 "account_code": "200", "tax_type": "GST on Income"}]}


def rows(content):
    return list(csv.reader(io.StringIO(content.decode("utf-8"))))


def test_seeded_format_matches_the_downloaded_xero_template(xero_format):
    assert ",".join(xero_format.headers) == TEMPLATE_HEADER
    assert xero_format.system == "xero" and "contact_name" in xero_format.required_fields


def test_build_csv_writes_one_row_per_line_in_template_order(xero_format):
    out = rows(build_csv(xero_format, [invoice()]))
    assert ",".join(out[0]) == TEMPLATE_HEADER
    assert len(out) == 3
    first = dict(zip(out[0], out[1]))
    assert first["*InvoiceNumber"] == "INV-9001" and first["*InvoiceDate"] == "13/10/2026"
    assert first["*UnitAmount"] == "4400.00" and first["TrackingOption1"] == "Gala"
    assert dict(zip(out[0], out[2]))["*Quantity"] == "21"


def test_build_csv_date_format(xero_format):
    out = rows(build_csv(xero_format, [invoice()], date_format="%Y-%m-%d"))
    assert dict(zip(out[0], out[1]))["*DueDate"] == "2026-10-27"


def test_build_csv_lists_every_missing_required_value(xero_format):
    bad = invoice(contact_name="", lines=[{"description": "Cleaning", "quantity": 1, "unit_amount": 1}])
    with pytest.raises(InvoiceFileError) as caught:
        build_csv(xero_format, [bad, invoice(number="INV-2", lines=[])])
    message = str(caught.value)
    assert "ContactName is required" in message and "AccountCode is required" in message
    assert "TaxType is required" in message and "INV-2 has no lines" in message


def test_setup_format_must_match_system(xero_format):
    org = Organisation.objects.create(name="Demo")
    setup = InvoiceSetup(organisation=org, name="Events", system="import", import_format=xero_format)
    with pytest.raises(ValidationError):
        setup.full_clean()


def test_items_take_their_setups_organisation(xero_format):
    org = Organisation.objects.create(name="Demo")
    setup = InvoiceSetup.objects.create(organisation=org, name="Events", import_format=xero_format)
    item = InvoiceItem.objects.create(setup=setup, name="Security", unit=InvoiceItem.Unit.HOUR, rate=Decimal("85"))
    assert item.organisation == org


@pytest.fixture
def admin_client(client, db):
    user = get_user_model().objects.create_superuser("admin", password="pw")
    client.force_login(user)
    return client


def test_organisation_page_shows_invoice_setups(admin_client, xero_format):
    org = Organisation.objects.create(name="TECC")
    InvoiceSetup.objects.create(organisation=org, name="Event invoices", import_format=xero_format)
    page = admin_client.get(f"/admin/core/organisation/{org.pk}/change/").content.decode()
    assert "Invoice set-ups" in page and "Event invoices" in page


def test_setup_can_be_added_with_items_in_admin(admin_client, xero_format):
    org = Organisation.objects.create(name="C&M")
    resp = admin_client.post("/admin/core/invoicesetup/add/", {
        "organisation": org.pk, "name": "Labour hire", "system": "xero", "import_format": xero_format.pk,
        "is_active": "on", "tax_basis": "exclusive", "date_format": "%d/%m/%Y", "due_days": 25,
        "items-TOTAL_FORMS": 1, "items-INITIAL_FORMS": 0, "items-MIN_NUM_FORMS": 0, "items-MAX_NUM_FORMS": 1000,
        "items-0-sort_order": 0, "items-0-name": "Day Labour", "items-0-unit": "hour", "items-0-rate": "80",
    })
    assert resp.status_code == 302, resp.content.decode()[:2000]
    item = InvoiceItem.objects.get()
    assert item.setup.name == "Labour hire" and item.organisation == org and item.rate == 80


def test_import_files_download_for_members_only(client, xero_format):
    member = get_user_model().objects.create_user("member", password="pw")
    outsider = get_user_model().objects.create_user("outsider", password="pw")
    org = Organisation.objects.create(name="Demo")
    org.members.add(member)
    f = ImportFile.objects.create(organisation=org, profile=xero_format.name, file_name="inv.csv",
                                  content=b"a,b\r\n", content_type="text/csv")
    client.force_login(outsider)
    assert client.get(f"/console/files/{f.pk}/download/").status_code == 404
    client.force_login(member)
    resp = client.get(f"/console/files/{f.pk}/download/")
    assert resp.content == b"a,b\r\n" and 'filename="inv.csv"' in resp["Content-Disposition"]
    log = AuditLog.objects.get(action=AuditLog.Action.DOWNLOAD)
    assert log.result["sha256"] == ImportFile.hash_bytes(b"a,b\r\n")


def test_sample_invoice_file_command(xero_format):
    Organisation.objects.create(name="Demo Company (AU)")
    call_command("sample_invoice_file", "--organisation", "demo company (au)", "--email", "test@example.com")
    f = ImportFile.objects.get()
    out = rows(bytes(f.content))
    assert len(out) == 7 and f.row_count == 6
    assert {r[10] for r in out[1:]} == {"ILA-TEST-001", "ILA-TEST-002"}
