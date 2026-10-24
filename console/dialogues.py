"""Guided conversations (/splittag, /tag, /createTag, /_createInvoice, a /reconcile working paper): plain input without
"/" answers the user's open one."""

from . import create_invoice, create_tag, reconcile, split_tag, tag_run


def open_dialogue(ctx):
    """(module, draft) for the user's most recently touched open conversation, or (None, None)."""
    drafts = [(module, module.open_draft(ctx)) for module in (split_tag, tag_run, create_tag, create_invoice, reconcile)]
    drafts = [(module, draft) for module, draft in drafts if draft is not None]
    if not drafts:
        return None, None
    return max(drafts, key=lambda pair: pair[1].updated_at)
