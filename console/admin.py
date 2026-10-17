from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import CommandReference, CustomCommand, TagRun


@admin.register(CustomCommand)
class CustomCommandAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation", "template", "is_active", "updated_at", "updated_by")
    list_filter = ("organisation", "template", "is_active")
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")


@admin.register(CommandReference)
class CommandReferenceAdmin(admin.ModelAdmin):
    """iLA's native commands. Everything but the instructions comes from code (refreshed on every migrate, or with
    manage.py sync_command_reference), so it always matches what the console accepts."""

    list_display = ("command", "summary", "aliases", "usage", "has_instructions", "in_code")
    list_filter = ("in_code", "needs_connection", "takes_input_file")
    search_fields = ("name", "aliases", "summary", "usage", "instructions")
    readonly_fields = ("command", "aliases", "summary", "usage", "parameter_table", "example_list",
                       "takes_input_file", "needs_connection", "in_code", "synced_at")
    fields = ("command", "summary", "usage", "parameter_table", "example_list", "aliases", "instructions",
              "takes_input_file", "needs_connection", "in_code", "synced_at")

    @admin.display(description="Command", ordering="sort_order")
    def command(self, obj):
        return f"/{obj.name}"

    @admin.display(description="How-to notes", boolean=True)
    def has_instructions(self, obj):
        return bool(obj.instructions)

    @admin.display(description="Parameters")
    def parameter_table(self, obj):
        if not obj.parameters:
            return "None"
        rows = format_html_join("", "<tr><td><code>{}</code></td><td>{}</td></tr>", obj.parameters)
        return format_html('<table><tr><th>Syntax</th><th>Meaning</th></tr>{}</table>', rows)

    @admin.display(description="Examples")
    def example_list(self, obj):
        return format_html_join(mark_safe("<br>"), "<code>{}</code>", ((e,) for e in obj.examples)) or "None"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TagRun)
class TagRunAdmin(admin.ModelAdmin):
    """Each /tag preview and what became of it. The tags themselves are under Core > Assignments."""

    list_display = ("created_at", "organisation", "user", "dimension", "status", "command", "rows", "approved")
    list_filter = ("organisation", "status", "dimension")
    readonly_fields = [f.name for f in TagRun._meta.fields]

    @admin.display(description="Command")
    def command(self, obj):
        return obj.data.get("command", "")

    @admin.display(description="Rows")
    def rows(self, obj):
        return len(obj.data.get("rows", []))

    @admin.display(description="Approved")
    def approved(self, obj):
        return len(obj.data.get("approved", []))

    def has_add_permission(self, request):
        return False

