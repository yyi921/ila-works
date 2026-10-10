import logging
import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from core.models import AuditLog
from xero.api import XeroAPIError
from xero.source import source_for

from .output import ExportError, export
from .parser import CommandSyntaxError, parse
from .periods import PeriodError
from .registry import COMMANDS, CommandError, Context, lookup

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
USER_ERRORS = (CommandSyntaxError, CommandError, XeroAPIError, ExportError, PeriodError)


def current_organisation(request):
    organisations = request.user.organisations.all()
    chosen = request.session.get("organisation_id")
    organisation = organisations.filter(pk=chosen).first() if chosen else None
    return organisation or organisations.order_by("name").first()


def _safe_filename(name):
    name = re.split(r"[\/]", name)[-1].strip()
    name = re.sub(r"[^\w.\- ]", "_", name)
    return name or "export"


@login_required
@require_GET
def console(request):
    organisation = current_organisation(request)
    return render(request, "console/console.html",
                  {"organisation": organisation, "command_names": sorted(COMMANDS)})


@login_required
@require_POST
def run(request):
    line = request.POST.get("command", "")
    upload = request.FILES.get("file")
    ctx = Context(current_organisation(request), request.user, source_factory=source_for, input_file=upload,
                  session=request.session)
    response, outcome, ok = None, {}, False
    try:
        cmd = parse(line)
        spec = lookup(cmd.name)
        if spec is None:
            raise CommandError(f"There's no /{cmd.name} command. Type /help to see them all.")
        if cmd.input_file and not spec.takes_input:
            raise CommandError(f"/{spec.name} doesn't take an input file (<<).")
        if spec.takes_input and cmd.input_file and upload is None:
            raise CommandError(f"Choose the file {cmd.input_file} to upload.")
        if upload is not None and upload.size > MAX_UPLOAD_BYTES:
            raise CommandError("Input files can be at most 10 MB.")
        result = spec.func(ctx, cmd)
        if cmd.output_file:
            filename = _safe_filename(cmd.output_file)
            content, content_type = export(result, filename)
            response = HttpResponse(content, content_type=content_type)
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            outcome = {"title": result.title, "file": filename}
        else:
            response = JsonResponse({"ok": True, "result": result.to_json(), "organisation": _org_name(ctx)})
            outcome = {"title": result.title}
        ok = True
    except USER_ERRORS as exc:
        outcome = {"error": str(exc)}
        response = JsonResponse({"ok": False, "error": str(exc), "organisation": _org_name(ctx)})
    except Exception:
        logger.exception("Command failed: %s", line[:200])
        outcome = {"error": "unexpected error"}
        response = JsonResponse({"ok": False, "error": "Something went wrong running that command. It has been "
                                                        "logged; please try again."}, status=500)
    if ctx.organisation is not None:
        AuditLog.objects.create(organisation=ctx.organisation, user=request.user, action=AuditLog.Action.COMMAND,
                                input=line[:2000], result=outcome, succeeded=ok)
    return response


def _org_name(ctx):
    return ctx.organisation.name if ctx.organisation else ""
