"""The builder page: where superusers (and users given "Can build custom commands") make an organisation's own
commands. Everything is scoped to the organisation in use, and every change is written to the audit log."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.models import AuditLog

from . import builder
from .forms import DEFAULTS, FORMS
from .models import CustomCommand
from .views import current_organisation


def builder_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not builder.can_build(request.user):
            raise PermissionDenied("Only superusers can use the builder.")
        organisation = current_organisation(request)
        if organisation is None:
            return render(request, "console/builder_home.html", {"organisation": None}, status=200)
        return view(request, organisation, *args, **kwargs)
    return wrapped


def _audit(request, organisation, custom, change, before=None):
    AuditLog.objects.create(
        organisation=organisation, user=request.user, action=AuditLog.Action.BUILD,
        input=f"{change} {custom.name}", target_type="console.customcommand", target_id=str(custom.pk),
        result={"before": before, "after": None if change == "delete" else _snapshot(custom)})


def _snapshot(custom):
    return {"name": custom.name, "template": custom.template, "summary": custom.summary,
            "definition": custom.definition, "is_active": custom.is_active}


@builder_required
def home(request, organisation):
    commands = CustomCommand.objects.filter(organisation=organisation).select_related("updated_by")
    return render(request, "console/builder_home.html",
                  {"organisation": organisation, "templates": builder.TEMPLATES, "commands": commands})


@builder_required
@require_http_methods(["GET", "POST"])
def new(request, organisation, template_key):
    template = builder.template(template_key)
    if template is None:
        raise Http404
    if not template.available:
        return render(request, "console/builder_home.html",
                      {"organisation": organisation, "templates": builder.TEMPLATES,
                       "commands": CustomCommand.objects.filter(organisation=organisation),
                       "message": f"The {template.title} template isn't available yet. {template.status_note}"},
                      status=409)
    form = FORMS[template.key](request.POST or None, organisation=organisation,
                               initial=DEFAULTS.get(template.key))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        custom = CustomCommand.objects.create(
            organisation=organisation, name=data["name"], template=template.key, summary=data["summary"],
            definition=form.definition(), is_active=data["is_active"],
            created_by=request.user, updated_by=request.user)
        _audit(request, organisation, custom, "create")
        return redirect("console:builder")
    return render(request, "console/builder_edit.html",
                  {"organisation": organisation, "form": form, "template": template, "custom": None})


@builder_required
@require_http_methods(["GET", "POST"])
def edit(request, organisation, pk):
    custom = get_object_or_404(CustomCommand, pk=pk, organisation=organisation)
    template = builder.template(custom.template)
    form = FORMS[custom.template](request.POST or None, organisation=organisation, instance=custom)
    if request.method == "POST" and form.is_valid():
        before = _snapshot(custom)
        data = form.cleaned_data
        custom.name, custom.summary, custom.is_active = data["name"], data["summary"], data["is_active"]
        custom.definition = {**custom.definition, **form.definition()}
        custom.updated_by = request.user
        custom.save()
        _audit(request, organisation, custom, "update", before)
        return redirect("console:builder")
    return render(request, "console/builder_edit.html",
                  {"organisation": organisation, "form": form, "template": template, "custom": custom})


@builder_required
@require_http_methods(["GET", "POST"])
def delete(request, organisation, pk):
    custom = get_object_or_404(CustomCommand, pk=pk, organisation=organisation)
    if request.method == "POST":
        before = _snapshot(custom)
        _audit(request, organisation, custom, "delete", before)
        custom.delete()
        return redirect("console:builder")
    return render(request, "console/builder_delete.html", {"organisation": organisation, "custom": custom})
