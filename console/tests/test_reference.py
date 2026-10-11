import pytest
from django.contrib.auth import get_user_model

from console.models import CommandReference
from console.reference import sync_command_reference
from console.registry import COMMANDS


@pytest.mark.django_db
def test_reference_matches_the_registry_and_keeps_edited_instructions():
    assert set(CommandReference.objects.values_list("name", flat=True)) == set(COMMANDS)  # filled on migrate
    compare = CommandReference.objects.get(name="compare")
    assert compare.usage == COMMANDS["compare"].usage and compare.parameters and compare.instructions
    compare.instructions = "Our own notes"
    compare.save()
    CommandReference.objects.create(name="retired", summary="x", usage="/retired")
    created, updated, retired = sync_command_reference()
    assert (created, retired) == (0, 1) and updated == len(COMMANDS)
    assert CommandReference.objects.get(name="compare").instructions == "Our own notes"
    assert not CommandReference.objects.get(name="retired").in_code


def test_admin_lists_and_shows_commands(client, db):
    client.force_login(get_user_model().objects.create_superuser("admin", password="pw"))
    page = client.get("/admin/console/commandreference/").content.decode()
    assert "/compare" in page and "/status" in page
    row = CommandReference.objects.get(name="bills")
    page = client.get(f"/admin/console/commandreference/{row.pk}/change/").content.decode()
    assert "--due &lt;days&gt;" in page and "payment run" in page
    assert client.get("/admin/console/commandreference/add/").status_code == 403
