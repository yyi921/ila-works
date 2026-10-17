"""Guided conversations (/tag, /createTag, /_createInvoice): plain input without "/" answers the user's open one."""

from . import create_invoice, create_tag, tag_run


def open_dialogue(ctx):
    """(module, draft) for the user's most recently touched open conversation, or (None, None)."""
    drafts = [(module, module.open_draft(ctx)) for module in (tag_run, create_tag, create_invoice)]
    drafts = [(module, draft) for module, draft in drafts if draft is not None]
    if not drafts:
        return None, None
    return max(drafts, key=lambda pair: pair[1].updated_at)
