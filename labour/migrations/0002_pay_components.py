"""The GL account comes from the pay component (Micropay's Pay Comp/Add Ded Code), not the cost account.

Pay components are made from the lines already uploaded. Each keeps the GL its lines went to before: the first
matching pay component rule, else the cost account's GL (the most common one among its lines)."""

from collections import Counter

import django.db.models.deletion
from django.db import migrations, models


def make_components(apps, schema_editor):
    PayLine = apps.get_model("labour", "PayLine")
    PayComponent = apps.get_model("labour", "PayComponent")
    ComponentRule = apps.get_model("labour", "ComponentRule")
    rules_by_org = {}
    for rule in ComponentRule.objects.order_by("order", "pk"):
        rules_by_org.setdefault(rule.organisation_id, []).append(rule)

    def by_rule(org_id, transaction_type, description):
        for rule in rules_by_org.get(org_id, []):
            text = rule.text.strip().lower()
            if rule.match_on == "transaction_type" and (transaction_type or "").strip().lower() == text:
                return rule.account
            if rule.match_on == "component" and text in (description or "").lower():
                return rule.account
        return ""

    found = {}
    for line in PayLine.objects.select_related("cost_account").order_by("pk"):
        key = (line.organisation_id, line.component_code or "(none)")
        entry = found.setdefault(key, {"description": line.component, "type": line.transaction_type,
                                       "gl": Counter()})
        entry["description"], entry["type"] = line.component or entry["description"], line.transaction_type
        gl = by_rule(line.organisation_id, line.transaction_type, line.component) or line.cost_account.gl_account
        if gl:
            entry["gl"][gl] += 1
    components = {}
    for (org_id, code), entry in found.items():
        components[(org_id, code)] = PayComponent.objects.create(
            organisation_id=org_id, code=code, description=entry["description"], transaction_type=entry["type"],
            gl_account=entry["gl"].most_common(1)[0][0] if entry["gl"] else "")
    for line in PayLine.objects.all():
        line.pay_component = components[(line.organisation_id, line.component_code or "(none)")]
        line.save(update_fields=["pay_component"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_event_dates"),
        ("labour", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PayComponent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_system", models.CharField(choices=[("xero", "Xero"), ("ila", "iLA"), ("import", "Customer file import")],
                                                   default="ila", max_length=20)),
                ("source_id", models.CharField(blank=True, max_length=255)),
                ("source_updated_at", models.DateTimeField(blank=True, null=True)),
                ("source_hash", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=50)),
                ("description", models.CharField(blank=True, help_text="e.g. Saturday Loading - 50%.",
                                                 max_length=255)),
                ("transaction_type", models.CharField(blank=True, help_text="As the last file had it, e.g. Super.",
                                                      max_length=100)),
                ("gl_account", models.CharField(blank=True, help_text="e.g. 6345.", max_length=20,
                                                verbose_name="GL account")),
                ("organisation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                                   to="core.organisation")),
            ],
            options={"verbose_name": "pay component", "ordering": ["code"], "abstract": False},
        ),
        migrations.AddConstraint(
            model_name="paycomponent",
            constraint=models.UniqueConstraint(condition=models.Q(("source_id", ""), _negated=True),
                                               fields=("organisation", "source_system", "source_id"),
                                               name="labour_paycomponent_source_uniq"),
        ),
        migrations.AddConstraint(
            model_name="paycomponent",
            constraint=models.UniqueConstraint(fields=("organisation", "code"), name="labour_paycomponent_code_uniq"),
        ),
        migrations.AddField(
            model_name="payline",
            name="pay_component",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="lines",
                                    to="labour.paycomponent"),
        ),
        migrations.RunPython(make_components, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="payline",
            name="pay_component",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines",
                                    to="labour.paycomponent"),
        ),
        migrations.RemoveField(model_name="costaccount", name="gl_account"),
        migrations.DeleteModel(name="ComponentRule"),
    ]
