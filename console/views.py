import logging
import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from core.models import AuditLog, ImportFile
from xero.api import XeroAPIError
from xero.source import source_for

from core.invoicing import InvoiceFileError

from . import builder, create_invoice, create_tag
from .dialogues import open_dialogue
from .llm import LLMError
from .models import CustomCommand, TagDraft
from .output import ExportError, export
from .parser import CHOSEN_FILE, CommandSyntaxError, parse
from .periods import PeriodError
from .registry import COMMANDS, CommandError, Context, lookup

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 25 * 1024 * 1024
USER_ERRORS = (CommandSyntaxError, CommandError, XeroAPIError, ExportError, PeriodError, LLMError, InvoiceFileError)


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
    built = []
    if organisation is not None:
        built = list(CustomCommand.objects.filter(organisation=organisation, is_active=True)
                     .values_list("name", flat=True))
    return render(request, "console/console.html",
                  {"organisation": organisation, "command_names": sorted(COMMANDS) + built,
                   "can_build": builder.can_build(request.user), "prefill": request.GET.get("cmd", "")[:200]})


@login_required
@require_POST
def run(request):
    line = request.POST.get("command", "")
    files = request.FILES.getlist("file")
    upload = files[0] if files else None
    ctx = Context(current_organisation(request), request.user, source_factory=source_for, input_file=upload,
                  session=request.session)
    response, outcome, ok = None, {}, False
    try:
        if sum(f.size for f in files) > MAX_UPLOAD_TOTAL_BYTES:
            raise CommandError("Choose at most 25 MB of files at a time.")
        cmd, result = _dispatch(ctx, line, files)
        if cmd is not None and cmd.output_file:
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


def _dispatch(ctx, line, files):
    """(parsed command or None, result). Plain input answers the user's open /createTag or invoice draft, if any."""
    if not line.lstrip().startswith(("/", "_")):
        module, draft = open_dialogue(ctx)
        if draft is not None:
            return None, module.answer(ctx, draft, line, files)
    cmd = parse(line)
    if len(files) > 1:
        raise CommandError("Choose one file for this command.")
    if cmd.name.startswith("_"):
        return cmd, _run_built(ctx, cmd)
    upload = files[0] if files else None
    spec = lookup(cmd.name)
    if spec is None:
        raise CommandError(f"There's no /{cmd.name} command. Type /help to see them all.")
    if cmd.input_file and not spec.takes_input:
        raise CommandError(f"/{spec.name} doesn't take an input file (<<).")
    if spec.takes_input and cmd.input_file and upload is None:
        raise CommandError(f"Choose {cmd.input_file} to upload." if cmd.input_file == CHOSEN_FILE
                           else f"Choose the file {cmd.input_file} to upload.")
    if upload is not None and upload.size > MAX_UPLOAD_BYTES:
        raise CommandError("Input files can be at most 10 MB.")
    return cmd, spec.func(ctx, cmd)


def _run_built(ctx, cmd):
    custom = builder.find(ctx.organisation, cmd.name)
    if custom is None:
        where = f" for {ctx.organisation.name}" if ctx.organisation else ""
        raise CommandError(f"There's no /{cmd.name} command{where}. Type /help to see the commands you can use.")
    if custom.template == CustomCommand.Template.CREATE_INVOICE:
        return create_invoice.start(ctx, cmd, custom)
    return builder.run(ctx, cmd, custom)


def _org_name(ctx):
    return ctx.organisation.name if ctx.organisation else ""


@login_required
@require_GET
def file_download(request, pk):
    """Download a stored import file. Members of the file's organisation only; every download is audit-logged."""
    import_file = get_object_or_404(ImportFile, pk=pk, organisation__members=request.user, content__isnull=False)
    AuditLog.objects.create(organisation=import_file.organisation, user=request.user, action=AuditLog.Action.DOWNLOAD,
                            input=f"download {import_file.file_name}", target_type="core.importfile",
                            target_id=str(import_file.pk), result={"sha256": ImportFile.hash_bytes(
                                bytes(import_file.content))})
    response = HttpResponse(bytes(import_file.content), content_type=import_file.content_type or "text/csv")
    response["Content-Disposition"] = f'attachment; filename="{_safe_filename(import_file.file_name)}"'
    return response


@login_required
@require_GET
def createtag_template(request, pk):
    """A starting values file for the user's own /createTag draft."""
    draft = get_object_or_404(TagDraft, pk=pk, user=request.user, organisation__members=request.user)
    name = _safe_filename(f"{draft.data.get('name') or 'dimension'} values.csv")
    response = HttpResponse(create_tag.template_csv(draft), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@login_required
@require_GET
def splittag_template(request):
    from .split_tag import template_csv

    response = HttpResponse(template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="splittag-template.csv"'
    return response
