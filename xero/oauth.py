"""Thin client for Xero's OAuth 2.0 endpoints."""

from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

TIMEOUT = 15


class XeroOAuthError(Exception):
    pass


def authorization_url(state):
    params = {
        "response_type": "code",
        "client_id": settings.XERO_CLIENT_ID,
        "redirect_uri": settings.XERO_REDIRECT_URI,
        "scope": settings.XERO_SCOPES,
        "state": state,
    }
    return f"{settings.XERO_AUTHORIZE_URL}?{urlencode(params)}"


def _token_request(data):
    resp = requests.post(
        settings.XERO_TOKEN_URL,
        data=data,
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise XeroOAuthError(f"Token request failed ({resp.status_code}): {resp.text[:200]}")
    token = resp.json()
    token["expires_at"] = timezone.now() + timedelta(seconds=int(token.get("expires_in", 1800)))
    return token


def exchange_code(code):
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.XERO_REDIRECT_URI}
    )


def refresh(connection):
    """Refresh the connection's tokens and save them. Xero rotates the refresh token on each use."""
    token = _token_request({"grant_type": "refresh_token", "refresh_token": connection.refresh_token})
    connection.access_token = token["access_token"]
    connection.refresh_token = token["refresh_token"]
    connection.expires_at = token["expires_at"]
    connection.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    return connection


def get_connections(access_token):
    resp = requests.get(
        settings.XERO_CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise XeroOAuthError(f"Connections request failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def revoke(refresh_token):
    """Revoke the refresh token, which disconnects every tenant authorised with it."""
    resp = requests.post(
        settings.XERO_REVOKE_URL,
        data={"token": refresh_token},
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise XeroOAuthError(f"Revoke failed ({resp.status_code}): {resp.text[:200]}")


def delete_connection(access_token, xero_connection_id):
    """Remove a single tenant connection without revoking the token."""
    resp = requests.delete(
        f"{settings.XERO_CONNECTIONS_URL}/{xero_connection_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    if resp.status_code not in (200, 204, 404):
        raise XeroOAuthError(f"Delete connection failed ({resp.status_code}): {resp.text[:200]}")
