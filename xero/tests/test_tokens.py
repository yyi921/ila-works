import uuid
from datetime import timedelta
from urllib.parse import parse_qs

import pytest
import responses
from django.conf import settings
from django.db import connection as db_connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from core import crypto
from core.models import AuditLog, Organisation
from xero import oauth
from xero.models import XeroConnection


@pytest.fixture
def conn(db):
    return XeroConnection.objects.create(
        organisation=Organisation.objects.create(name="Demo Co"), tenant_id=uuid.uuid4(),
        access_token="access-1", refresh_token="refresh-1", expires_at=timezone.now() + timedelta(minutes=30),
    )


def expire(connection):
    XeroConnection.objects.filter(pk=connection.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    connection.refresh_from_db()


def test_default_scopes_are_read_only_and_granular():
    scopes = settings.XERO_SCOPES.split()
    assert "offline_access" in scopes
    accounting = [s for s in scopes if s.startswith("accounting.")]
    assert accounting and all(s.endswith(".read") for s in accounting)
    assert "accounting.transactions" not in scopes and "accounting.transactions.read" not in scopes


@responses.activate
def test_fresh_token_is_returned_without_calling_xero(conn):
    assert oauth.get_access_token(conn) == "access-1"
    assert len(responses.calls) == 0


@responses.activate
def test_expired_token_is_refreshed_and_rotated(conn):
    expire(conn)
    responses.post(settings.XERO_TOKEN_URL, json={"access_token": "access-2", "refresh_token": "refresh-2",
                                                  "expires_in": 1800})

    assert oauth.get_access_token(conn) == "access-2"

    body = parse_qs(responses.calls[0].request.body)
    assert body == {"grant_type": ["refresh_token"], "refresh_token": ["refresh-1"]}
    stored = XeroConnection.objects.get(pk=conn.pk)
    assert (stored.access_token, stored.refresh_token) == ("access-2", "refresh-2")
    assert not stored.is_access_token_expired()


@responses.activate
def test_stale_copy_does_not_spend_the_rotated_refresh_token(conn):
    """A second worker holding an outdated copy must reuse the new token, not refresh again."""
    stale = XeroConnection.objects.get(pk=conn.pk)
    expire(conn)
    expire(stale)
    responses.post(settings.XERO_TOKEN_URL, json={"access_token": "access-2", "refresh_token": "refresh-2",
                                                  "expires_in": 1800})
    oauth.get_access_token(conn)

    assert oauth.get_access_token(stale) == "access-2"
    assert len(responses.calls) == 1


@responses.activate
def test_invalid_grant_marks_connection_revoked(conn):
    expire(conn)
    responses.post(settings.XERO_TOKEN_URL, status=400, json={"error": "invalid_grant"})

    with pytest.raises(oauth.XeroConnectionRevoked):
        oauth.get_access_token(conn)

    stored = XeroConnection.objects.get(pk=conn.pk)
    assert stored.status == XeroConnection.Status.REVOKED
    assert stored.access_token == stored.refresh_token == ""
    assert AuditLog.objects.filter(organisation=conn.organisation, action="disconnect").exists()


@responses.activate
def test_other_token_errors_do_not_revoke(conn):
    expire(conn)
    responses.post(settings.XERO_TOKEN_URL, status=500, body="upstream error")
    with pytest.raises(oauth.XeroOAuthError) as exc:
        oauth.get_access_token(conn)
    assert not isinstance(exc.value, oauth.XeroConnectionRevoked)
    assert XeroConnection.objects.get(pk=conn.pk).status == XeroConnection.Status.ACTIVE


@responses.activate
def test_revoked_connection_never_calls_xero(conn):
    oauth.mark_revoked(conn, reason="test")
    with pytest.raises(oauth.XeroConnectionRevoked):
        oauth.get_access_token(conn)
    assert len(responses.calls) == 0


@responses.activate
def test_error_messages_never_contain_tokens(conn):
    expire(conn)
    responses.post(settings.XERO_TOKEN_URL, status=401, body="refresh-1 access-1")
    with pytest.raises(oauth.XeroOAuthError) as exc:
        oauth.get_access_token(conn)
    assert "refresh-1" not in str(exc.value) and "access-1" not in str(exc.value)


@pytest.mark.django_db(transaction=True)
def test_migration_encrypts_existing_plaintext_tokens():
    executor = MigrationExecutor(db_connection)
    executor.migrate([("xero", "0001_initial")])
    org = Organisation.objects.create(name="Legacy Co")
    with db_connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO xero_xeroconnection (organisation_id, tenant_id, tenant_name, tenant_type, access_token,"
            " refresh_token, id_token, scopes, expires_at, created_at, updated_at)"
            " VALUES (%s, %s, '', '', 'plain-access', 'plain-refresh', '', '', %s, %s, %s)",
            [org.pk, uuid.uuid4().hex, timezone.now(), timezone.now(), timezone.now()],
        )

    executor = MigrationExecutor(db_connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())

    with db_connection.cursor() as cursor:
        cursor.execute("SELECT access_token, refresh_token FROM xero_xeroconnection")
        access, refresh = cursor.fetchone()
    assert crypto.decrypt(access) == "plain-access"
    assert crypto.decrypt(refresh) == "plain-refresh"
    stored = XeroConnection.objects.get()
    assert stored.access_token == "plain-access"
    assert stored.status == XeroConnection.Status.ACTIVE
