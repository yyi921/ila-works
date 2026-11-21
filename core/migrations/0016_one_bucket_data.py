from django.db import migrations


def to_one_bucket(apps, schema_editor):
    """One bucket, two statuses: Expected journals become Drafts; superseded, rejected and posted ones have left the
    bucket. Numbers already given out are never used again."""
    Adjustment = apps.get_model("core", "Adjustment")
    Counter = apps.get_model("core", "OverlayCounter")
    last = {}
    for org, number in Adjustment.objects.values_list("organisation_id", "number"):
        last[org] = max(last.get(org, 0), number)
    for org, number in last.items():
        Counter.objects.update_or_create(organisation_id=org, defaults={"last": number})
    Adjustment.objects.filter(status__in=("superseded", "rejected", "posted")).delete()
    Adjustment.objects.filter(status="expected").update(status="draft", made_by=None)



class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_one_bucket'),
    ]

    operations = [
        migrations.RunPython(to_one_bucket, migrations.RunPython.noop),
    ]
