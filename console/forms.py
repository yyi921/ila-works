from django import forms

from . import builder
from .models import CustomCommand


class CommandForm(forms.Form):
    """What every built command has: a name, what it's for, and whether it can be run."""

    name = forms.CharField(max_length=32, help_text='Starts with "_". Letters, digits, "-" and "_"; capitals are '
                                                    "kept for display, but typing it ignores them.")
    summary = forms.CharField(max_length=200, label="What it does",
                              help_text="Shown in /help and on the builder page.")
    is_active = forms.BooleanField(required=False, initial=True, label="Active (anyone in the organisation can run it)")

    def __init__(self, *args, organisation, instance=None, **kwargs):
        self.organisation, self.instance = organisation, instance
        if instance is not None:
            kwargs["initial"] = self.initial_from(instance)
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)

    @staticmethod
    def initial_from(instance):
        return {"name": instance.name, "summary": instance.summary, "is_active": instance.is_active}

    def definition(self):
        return {}

    def clean_name(self):
        try:
            name = builder.clean_name(self.cleaned_data["name"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from None
        clash = CustomCommand.objects.filter(organisation=self.organisation, name__iexact=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"{self.organisation.name} already has a command called {name}.")
        return name


class ShortcutForm(CommandForm):
    """A "start from scratch" command: the iLA commands it runs, in order."""

    steps = forms.CharField(widget=forms.Textarea(attrs={"rows": 6, "spellcheck": "false"}), label="Commands",
                            help_text="One iLA command per line, run in order. {text} and {params} take what is "
                                      "typed after the name.")
    field_order = ["name", "summary", "steps", "is_active"]

    @staticmethod
    def initial_from(instance):
        return {**CommandForm.initial_from(instance), "steps": "\n".join(instance.steps)}

    def definition(self):
        return {"steps": self.cleaned_data["steps"]}

    def clean_steps(self):
        try:
            return builder.clean_steps(self.cleaned_data["steps"])
        except ValueError as exc:
            raise forms.ValidationError(list(exc.args)) from None


class CreateInvoiceForm(CommandForm):
    """A command made from the createInvoice template. How invoices are built is set per organisation in its
    invoice set-ups, so the command itself only needs a name."""


FORMS = {CustomCommand.Template.SHORTCUT: ShortcutForm, CustomCommand.Template.CREATE_INVOICE: CreateInvoiceForm}
DEFAULTS = {
    CustomCommand.Template.CREATE_INVOICE: {
        "name": "_createInvoice", "is_active": True,
        "summary": "Build a customer invoice as an import file, step by step, from the billing information you give.",
    },
}
