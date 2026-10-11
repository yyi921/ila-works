from django.db import migrations


def add_xero_format(apps, schema_editor):
    from core.invoicing import XERO_SALES_INVOICE_COLUMNS, XERO_SALES_INVOICE_NOTES

    apps.get_model("core", "ImportFormat").objects.update_or_create(
        name="Xero sales invoice CSV",
        defaults={"system": "xero", "document_type": "sales_invoice", "columns": XERO_SALES_INVOICE_COLUMNS,
                  "file_type": "csv", "notes": XERO_SALES_INVOICE_NOTES},
    )


def remove_xero_format(apps, schema_editor):
    apps.get_model("core", "ImportFormat").objects.filter(name="Xero sales invoice CSV", setups__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0004_invoice_setup")]
    operations = [migrations.RunPython(add_xero_format, remove_xero_format)]
