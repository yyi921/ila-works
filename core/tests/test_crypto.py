import io
import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection as db_connection
from django.utils import timezone

from core import crypto
from core.models import Organisation
from xero.models import XeroConnection


def raw_token(pk):
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT access_token FROM xero_xeroconnection WHERE id = %s", [pk])
        return cursor.fetchone()[0]


@pytest.fixture
def xero_connection(db):
    return XeroConnection.objects.create(
        organisation=Organisation.objects.create(name="Demo Co"), tenant_id=uuid.uuid4(),
        access_token="secret-access", refresh_token="secret-refresh", expires_at=timezone.now() + timedelta(minutes=30),
    )


def test_round_trip():
    assert crypto.decrypt(crypto.encrypt("hello")) == "hello"


def test_tokens_are_encrypted_in_the_database(xero_connection):
    stored = raw_token(xero_connection.pk)
    assert stored != "secret-access"
    assert "secret-access" not in stored
    assert XeroConnection.objects.get(pk=xero_connection.pk).access_token == "secret-access"


def test_empty_values_stay_empty(xero_connection):
    assert XeroConnection.objects.get(pk=xero_connection.pk).id_token == ""


def test_wrong_key_raises_without_leaking_value(settings):
    ciphertext = crypto.encrypt("secret-access")
    settings.FIELD_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    with pytest.raises(crypto.DecryptionError) as exc:
        crypto.decrypt(ciphertext)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("keys", [[], ["not-a-fernet-key"]])
def test_bad_key_config(settings, keys):
    settings.FIELD_ENCRYPTION_KEYS = keys
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_key_rotation(settings, xero_connection):
    old_key = settings.FIELD_ENCRYPTION_KEYS[0]
    new_key = Fernet.generate_key().decode()

    settings.FIELD_ENCRYPTION_KEYS = [new_key, old_key]
    assert XeroConnection.objects.get(pk=xero_connection.pk).access_token == "secret-access"
    out = io.StringIO()
    call_command("rotate_encryption_keys", stdout=out)
    assert "xero.XeroConnection: re-encrypted 1 row(s)" in out.getvalue()

    settings.FIELD_ENCRYPTION_KEYS = [new_key]
    reloaded = XeroConnection.objects.get(pk=xero_connection.pk)
    assert reloaded.access_token == "secret-access"
    assert reloaded.refresh_token == "secret-refresh"
