import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.models import AuditLog, Organisation, SourceSystem

from . import oauth
from .models import XeroConnection

logger = logging.getLogger(__name__)

SESSION_STATE_KEY = "xero_oauth_state"


def _user_connections(user):
    return XeroConnection.objects.filter(organisation__members=user).select_related("organisation")


@login_required
@require_GET
def status(request):
    return render(request, "xero/status.html", {"connections": _user_connections(request.user).order_by("tenant_name")})


@login_required
@require_GET
def connect(request):
    state = secrets.token_urlsafe(32)
    request.session[SESSION_STATE_KEY] = state
    return redirect(oauth.authorization_url(state))


@login_required
@require_GET
def callback(request):
    expected_state = request.session.pop(SESSION_STATE_KEY, None)
    if request.GET.get("error"):
        messages.error(request, f"Xero authorisation was not completed: {request.GET['error']}")
        return redirect("xero:status")
    state = request.GET.get("state")
    code = request.GET.get("code")
    if not expected_state or not state or not secrets.compare_digest(state, expected_state):
        messages.error(request, "Invalid OAuth state. Please try connecting again.")
        return redirect("xero:status")
    if not code:
        messages.error(request, "Xero did not return an authorisation code.")
        return redirect("xero:status")

    try:
        token = oauth.exchange_code(code)
        tenants = oauth.get_connections(token["access_token"])
    except oauth.XeroOAuthError:
        logger.exception("Xero OAuth callback failed")
        messages.error(request, "Could not complete the Xero connection. Please try again.")
        return redirect("xero:status")

    token_fields = {
        "status": XeroConnection.Status.ACTIVE,
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "id_token": token.get("id_token", ""),
        "scopes": token.get("scope", ""),
        "expires_at": token["expires_at"],
    }
    connected = []
    with transaction.atomic():
        for tenant in tenants:
            name = tenant.get("tenantName") or ""
            connection = XeroConnection.objects.filter(tenant_id=tenant["tenantId"]).first()
            if connection is None:
                connection = XeroConnection(tenant_id=tenant["tenantId"],
                                            organisation=_unconnected_organisation(request.user, name))
            elif not connection.organisation.members.filter(pk=request.user.pk).exists():
                # Already connected under another iLA account; don't hand this user access to its data.
                messages.error(request, f"{name} is already connected by another user.")
                continue
            if connection.status != XeroConnection.Status.ACTIVE:
                connection.status_changed_at = timezone.now()
            connection.tenant_name = name
            connection.tenant_type = tenant.get("tenantType") or ""
            connection.xero_connection_id = tenant.get("id")
            for field, value in token_fields.items():
                setattr(connection, field, value)
            connection.save()
            connection.organisation.members.add(request.user)
            connected.append(connection)
            AuditLog.objects.create(
                organisation=connection.organisation,
                source_system=SourceSystem.ILA,
                user=request.user,
                action=AuditLog.Action.CONNECT,
                input="connect xero",
                result={"tenant_id": str(connection.tenant_id), "tenant_name": name},
            )
    for connection in connected:
        _load_chart_of_accounts(connection)
    messages.success(request, "Xero connection updated.")
    return redirect("xero:status")


def _load_chart_of_accounts(connection):
    """Load the chart of accounts the first time an organisation connects; after that an admin refreshes it. A
    failure here doesn't undo the connection: the chart then loads on first use."""
    from core.models import Account

    from .source import XeroSource
    from .sync import refresh_accounts

    if Account.objects.filter(organisation=connection.organisation).exists():
        return
    try:
        refresh_accounts(connection.organisation, XeroSource(connection), fresh=True)
    except Exception:
        logger.warning("Couldn't load the chart of accounts for %s", connection.organisation, exc_info=True)


def _unconnected_organisation(user, name):
    """The organisation for a newly connected tenant: one of the user's own with the same name and no Xero
    connection (left from before disconnects kept their record), else a new one."""
    existing = (Organisation.objects.filter(members=user, name__iexact=name, xero_connection__isnull=True)
                .order_by("pk").first() if name else None)
    return existing or Organisation.objects.create(name=name or "Xero organisation")


@login_required
@require_POST
def disconnect(request, pk):
    connection = get_object_or_404(_user_connections(request.user), pk=pk)
    result = {"tenant_id": str(connection.tenant_id)}
    try:
        if connection.is_active and connection.xero_connection_id:
            oauth.delete_connection(oauth.get_access_token(connection), connection.xero_connection_id)
        result["xero_confirmed"] = True
    except oauth.XeroConnectionRevoked:
        result["xero_confirmed"] = True  # already gone at Xero's end
    except oauth.XeroOAuthError:
        # Remove it locally anyway; the user can also revoke the app from Xero's Connected Apps page.
        logger.exception("Failed to disconnect tenant %s at Xero", connection.tenant_id)
        messages.warning(request, "Xero did not confirm the disconnect; removed locally.")
        result["xero_confirmed"] = False
    # Keep the connection record (without tokens) so reconnecting the same tenant reuses its Organisation.
    # Synced data stays with the Organisation; deletion is a separate offboarding step.
    organisation, name = connection.organisation, str(connection)
    connection.status = XeroConnection.Status.DISCONNECTED
    connection.status_changed_at = timezone.now()
    connection.access_token = connection.refresh_token = connection.id_token = ""
    connection.xero_connection_id = None
    connection.save()
    AuditLog.objects.create(
        organisation=organisation, user=request.user, action=AuditLog.Action.DISCONNECT,
        input="disconnect xero", result=result,
    )
    messages.success(request, f"Disconnected {name}.")
    return redirect("xero:status")
