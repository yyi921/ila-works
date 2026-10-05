"""Thin client for Xero's OAuth 2.0 endpoints. Tokens must never be logged or put in exception messages."""

import logging
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog

from .models import XeroConnection

logger = logging.getLogger(__name__)

TIMEOUT = 15


class XeroOAuthError(Exception):
    pass


class XeroConnectionRevoked(XeroOAuthError):
    """The refresh token was rejected: the user disconnected the app in Xero, or it lapsed after 60 days unused."""


def authorization_url(state):
    params = {
        "response_type": "code",
        "client_id": settings.XERO_CLIENT_ID,
        "redirect_uri": settings.XERO_REDIRECT_URI,
        "scope": settings.XERO_SCOPES,
        "state": state,
    }
    return f"{settings.XERO_AUTHORIZE_URL}?{urlencode(params)}"


def _error_code(resp):
    try:
        return resp.json().get("error", "")
    except ValueError:
        return ""


def _token_request(data):
    resp = requests.post(
        settings.XERO_TOKEN_URL,
        data=data,
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        error = _error_code(resp)
        if error == "invalid_grant" and data["grant_type"] == "refresh_token":
            raise XeroConnectionRevoked("Xero rejected the refresh token (invalid_grant).")
        raise XeroOAuthError(f"Token request failed ({resp.status_code} {error or 'no error code'}).")
    token = resp.json()
    token["expires_at"] = timezone.now() + timedelta(seconds=int(token.get("expires_in", 1800)))
    return token


def exchange_code(code):
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.XERO_REDIRECT_URI}
    )


def get_access_token(connection):
    """Return a valid access token for ``connection``, refreshing it first if needed.

    Xero rotates the refresh token on every refresh, so the refresh runs under a row lock: a second worker
    waits, then sees the fresh token instead of spending the old one. Raises XeroConnectionRevoked (after
    marking the connection revoked) if Xero no longer accepts the refresh token.
    """
    if not connection.is_active:
        raise XeroConnectionRevoked(f"{connection} is disconnected; reconnect to Xero.")
    if not connection.is_access_token_expired():
        return connection.access_token
    try:
        with transaction.atomic():
            locked = XeroConnection.objects.select_for_update().get(pk=connection.pk)
            if locked.is_access_token_expired():
                token = _token_request({"grant_type": "refresh_token", "refresh_token": locked.refresh_token})
                locked.access_token = token["access_token"]
                locked.refresh_token = token["refresh_token"]
                locked.expires_at = token["expires_at"]
                locked.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    except XeroConnectionRevoked:
        mark_revoked(connection, reason="refresh token rejected")
        raise
    connection.refresh_from_db(fields=["access_token", "refresh_token", "expires_at", "status"])
    return connection.access_token


def mark_revoked(connection, *, reason):
    """Record that Xero no longer honours this connection. Tokens are wiped; synced data is kept."""
    XeroConnection.objects.filter(pk=connection.pk).update(
        status=XeroConnection.Status.REVOKED, status_changed_at=timezone.now(), access_token="", refresh_token="",
        id_token="",
    )
    connection.refresh_from_db()
    logger.warning("Xero connection %s marked revoked: %s", connection.tenant_id, reason)
    AuditLog.objects.create(
        organisation=connection.organisation, action=AuditLog.Action.DISCONNECT, succeeded=True,
        input="xero connection revoked", result={"tenant_id": str(connection.tenant_id), "reason": reason},
    )


def get_connections(access_token):
    resp = requests.get(
        settings.XERO_CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise XeroOAuthError(f"Connections request failed ({resp.status_code}).")
    return resp.json()


def delete_connection(access_token, xero_connection_id):
    """Remove a single tenant connection without revoking the token."""
    resp = requests.delete(
        f"{settings.XERO_CONNECTIONS_URL}/{xero_connection_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    if resp.status_code not in (200, 204, 404):
        raise XeroOAuthError(f"Delete connection failed ({resp.status_code}).")
