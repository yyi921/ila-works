import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, Organisation
from xero.models import XeroConnection
from xero.views import SESSION_STATE_KEY

TENANT_ID = str(uuid.uuid4())
CONNECTION_ID = str(uuid.uuid4())


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="alice", password="pw")


@pytest.fixture
def user_client(client, user):
    client.force_login(user)
    return client


def _set_state(client, state="expected-state"):
    session = client.session
    session[SESSION_STATE_KEY] = state
    session.save()
    return state


def _token_payload(**overrides):
    payload = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "id-1",
        "expires_in": 1800,
        "token_type": "Bearer",
        "scope": settings.XERO_SCOPES,
    }
    payload.update(overrides)
    return payload


def _make_connection(member=None, **overrides):
    organisation = Organisation.objects.create(name="Demo Co")
    if member:
        organisation.members.add(member)
    fields = {
        "organisation": organisation,
        "tenant_id": TENANT_ID,
        "tenant_name": "Demo Co",
        "xero_connection_id": CONNECTION_ID,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": timezone.now() + timedelta(minutes=30),
    }
    fields.update(overrides)
    return XeroConnection.objects.create(**fields)


def test_callback_url_path():
    assert reverse("xero:callback") == "/xero/callback/"


@pytest.mark.django_db
def test_views_require_login(client):
    assert client.get(reverse("xero:connect")).status_code == 302
    assert "/admin/login/" in client.get(reverse("xero:connect"))["Location"]


@pytest.mark.django_db
def test_connect_redirects_to_xero_with_state(user_client):
    resp = user_client.get(reverse("xero:connect"))
    assert resp.status_code == 302
    url = urlparse(resp["Location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == settings.XERO_AUTHORIZE_URL
    qs = parse_qs(url.query)
    assert qs["client_id"] == [settings.XERO_CLIENT_ID]
    assert qs["redirect_uri"] == [settings.XERO_REDIRECT_URI]
    assert qs["response_type"] == ["code"]
    assert "offline_access" in qs["scope"][0]
    assert qs["state"] == [user_client.session[SESSION_STATE_KEY]]


@pytest.mark.django_db
@responses.activate
def test_callback_exchanges_code_and_stores_connection(user_client):
    state = _set_state(user_client)
    responses.post(settings.XERO_TOKEN_URL, json=_token_payload())
    responses.get(
        settings.XERO_CONNECTIONS_URL,
        json=[{"id": CONNECTION_ID, "tenantId": TENANT_ID, "tenantName": "Demo Co", "tenantType": "ORGANISATION"}],
    )

    resp = user_client.get(reverse("xero:callback"), {"code": "abc", "state": state})

    assert resp.status_code == 302
    assert resp["Location"] == reverse("xero:status")
    conn = XeroConnection.objects.get()
    assert str(conn.tenant_id) == TENANT_ID
    assert conn.tenant_name == "Demo Co"
    assert conn.refresh_token == "refresh-1"
    assert conn.expires_at > timezone.now()
    token_body = parse_qs(responses.calls[0].request.body)
    assert token_body["grant_type"] == ["authorization_code"]
    assert token_body["code"] == ["abc"]
    assert SESSION_STATE_KEY not in user_client.session
    assert conn.organisation.name == "Demo Co"
    assert conn.organisation.members.filter(username="alice").exists()
    assert AuditLog.objects.filter(organisation=conn.organisation, action="connect").count() == 1


@pytest.mark.django_db
@responses.activate
def test_callback_rejects_bad_state(user_client):
    _set_state(user_client, "expected-state")
    resp = user_client.get(reverse("xero:callback"), {"code": "abc", "state": "forged"})
    assert resp.status_code == 302
    assert not XeroConnection.objects.exists()
    assert len(responses.calls) == 0


@pytest.mark.django_db
@responses.activate
def test_callback_handles_token_failure(user_client):
    state = _set_state(user_client)
    responses.post(settings.XERO_TOKEN_URL, status=400, json={"error": "invalid_grant"})
    resp = user_client.get(reverse("xero:callback"), {"code": "abc", "state": state})
    assert resp.status_code == 302
    assert not XeroConnection.objects.exists()


@pytest.mark.django_db
def test_callback_handles_user_denial(user_client):
    _set_state(user_client)
    resp = user_client.get(reverse("xero:callback"), {"error": "access_denied"})
    assert resp.status_code == 302
    assert not XeroConnection.objects.exists()


@pytest.mark.django_db
@responses.activate
def test_disconnect_removes_at_xero_and_keeps_record_without_tokens(user_client, user):
    conn = _make_connection(user)
    responses.delete(f"{settings.XERO_CONNECTIONS_URL}/{CONNECTION_ID}", status=204)

    resp = user_client.post(reverse("xero:disconnect", args=[conn.pk]))

    assert resp.status_code == 302
    conn.refresh_from_db()
    assert conn.status == XeroConnection.Status.DISCONNECTED and not conn.is_active
    assert (conn.access_token, conn.refresh_token, conn.id_token, conn.xero_connection_id) == ("", "", "", None)
    assert Organisation.objects.count() == 1  # synced data is kept
    assert responses.calls[0].request.headers["Authorization"] == "Bearer access-1"
    assert AuditLog.objects.filter(action="disconnect").count() == 1


@pytest.mark.django_db
@responses.activate
def test_disconnect_refreshes_expired_token_first(user_client, user):
    conn = _make_connection(user, expires_at=timezone.now() - timedelta(minutes=1))
    responses.post(settings.XERO_TOKEN_URL, json=_token_payload(access_token="access-2", refresh_token="refresh-2"))
    responses.delete(f"{settings.XERO_CONNECTIONS_URL}/{CONNECTION_ID}", status=204)

    user_client.post(reverse("xero:disconnect", args=[conn.pk]))

    assert responses.calls[1].request.headers["Authorization"] == "Bearer access-2"
    assert XeroConnection.objects.get().status == XeroConnection.Status.DISCONNECTED


@pytest.mark.django_db
def test_disconnect_requires_post(user_client, user):
    conn = _make_connection(user)
    assert user_client.get(reverse("xero:disconnect", args=[conn.pk])).status_code == 405
    assert XeroConnection.objects.filter(pk=conn.pk).exists()


@pytest.mark.django_db
def test_cannot_disconnect_another_users_organisation(user_client, django_user_model):
    other = django_user_model.objects.create_user(username="bob", password="pw")
    conn = _make_connection(other)
    assert user_client.post(reverse("xero:disconnect", args=[conn.pk])).status_code == 404
    assert XeroConnection.objects.filter(pk=conn.pk).exists()


@pytest.mark.django_db
@responses.activate
def test_callback_does_not_grant_access_to_tenant_owned_by_another_user(user_client, django_user_model):
    other = django_user_model.objects.create_user(username="bob", password="pw")
    conn = _make_connection(other)
    state = _set_state(user_client)
    responses.post(settings.XERO_TOKEN_URL, json=_token_payload(access_token="mine"))
    responses.get(settings.XERO_CONNECTIONS_URL, json=[{"id": CONNECTION_ID, "tenantId": TENANT_ID, "tenantName": "Demo Co"}])

    user_client.get(reverse("xero:callback"), {"code": "abc", "state": state})

    conn.refresh_from_db()
    assert conn.access_token == "access-1"
    assert not conn.organisation.members.filter(username="alice").exists()


@pytest.mark.django_db
@responses.activate
def test_disconnect_revoked_connection_skips_xero(user_client, user):
    conn = _make_connection(user, status=XeroConnection.Status.REVOKED, access_token="", refresh_token="")
    resp = user_client.post(reverse("xero:disconnect", args=[conn.pk]))
    assert resp.status_code == 302
    assert len(responses.calls) == 0
    assert XeroConnection.objects.get().status == XeroConnection.Status.DISCONNECTED


@pytest.mark.django_db
@responses.activate
def test_reconnect_reactivates_revoked_connection(user_client, user):
    conn = _make_connection(user, status=XeroConnection.Status.REVOKED, access_token="", refresh_token="")
    state = _set_state(user_client)
    responses.post(settings.XERO_TOKEN_URL, json=_token_payload(access_token="fresh", refresh_token="fresh-r"))
    responses.get(settings.XERO_CONNECTIONS_URL, json=[{"id": CONNECTION_ID, "tenantId": TENANT_ID, "tenantName": "Demo Co"}])

    user_client.get(reverse("xero:callback"), {"code": "abc", "state": state})

    conn.refresh_from_db()
    assert conn.status == XeroConnection.Status.ACTIVE
    assert (conn.access_token, conn.refresh_token) == ("fresh", "fresh-r")
    assert Organisation.objects.count() == 1


def _reconnect(client, tenant_name="Demo Co"):
    state = _set_state(client)
    responses.post(settings.XERO_TOKEN_URL, json=_token_payload(access_token="fresh", refresh_token="fresh-r"))
    responses.get(settings.XERO_CONNECTIONS_URL,
                  json=[{"id": CONNECTION_ID, "tenantId": TENANT_ID, "tenantName": tenant_name}])
    client.get(reverse("xero:callback"), {"code": "abc", "state": state})


@pytest.mark.django_db
@responses.activate
def test_disconnect_then_reconnect_reuses_the_organisation(user_client, user):
    conn = _make_connection(user)
    responses.delete(f"{settings.XERO_CONNECTIONS_URL}/{CONNECTION_ID}", status=204)
    user_client.post(reverse("xero:disconnect", args=[conn.pk]))

    _reconnect(user_client)

    assert Organisation.objects.count() == 1 and XeroConnection.objects.count() == 1
    conn.refresh_from_db()
    assert conn.is_active and conn.access_token == "fresh"


@pytest.mark.django_db
@responses.activate
def test_reconnect_reuses_own_unconnected_organisation_with_same_name(user_client, user):
    orphan = Organisation.objects.create(name="Demo Co")  # left by a disconnect before records were kept
    orphan.members.add(user)

    _reconnect(user_client, tenant_name="DEMO CO")

    assert Organisation.objects.count() == 1
    assert XeroConnection.objects.get().organisation == orphan


@pytest.mark.django_db
@responses.activate
def test_reconnect_never_reuses_another_users_organisation(user_client, django_user_model):
    other = django_user_model.objects.create_user(username="bob", password="pw")
    theirs = Organisation.objects.create(name="Demo Co")
    theirs.members.add(other)

    _reconnect(user_client)

    conn = XeroConnection.objects.get()
    assert conn.organisation != theirs and Organisation.objects.count() == 2


@pytest.mark.django_db
def test_status_page_hides_disconnect_for_disconnected(user_client, user):
    _make_connection(user, status=XeroConnection.Status.DISCONNECTED, access_token="", refresh_token="")
    page = user_client.get(reverse("xero:status")).content.decode()
    assert "Disconnected in iLA" in page and "Reconnect" in page and "<button" not in page
