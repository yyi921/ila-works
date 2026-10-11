"""The language model, used only to read documents and understand requests. It returns structured data through a
forced tool call; iLA checks and calculates everything that matters (prices, totals, codes) itself. Prompts and
documents are never logged."""

import base64
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000


class LLMError(Exception):
    """Message is shown to the user as is."""


def available():
    return bool(settings.ANTHROPIC_API_KEY)


def _client():
    if not available():
        raise LLMError("Reading documents needs the language model, which isn't set up: add ANTHROPIC_API_KEY to "
                       "the server's settings (.env locally) and restart.")
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def document_block(name, content, content_type):
    """A message content block for one uploaded file: PDFs and images are sent as they are, spreadsheets and text
    as text."""
    lower = name.lower()
    if lower.endswith(".pdf") or content_type == "application/pdf":
        return {"type": "document", "title": name,
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(content).decode()}}
    for ext, media in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg")):
        if lower.endswith(ext):
            return {"type": "image", "source": {"type": "base64", "media_type": media,
                                                "data": base64.standard_b64encode(content).decode()}}
    if lower.endswith((".xlsx", ".xlsm")):
        return {"type": "text", "text": f"File {name} (spreadsheet):\n{spreadsheet_text(content)}"}
    if lower.endswith((".csv", ".txt", ".tsv")):
        return {"type": "text", "text": f"File {name}:\n{content.decode('utf-8-sig', errors='replace')}"}
    raise LLMError(f"{name}: iLA can read PDF, Excel (.xlsx), CSV, text and image files.")


def spreadsheet_text(content):
    import io

    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts = []
    for sheet in book.worksheets:
        parts.append(f"--- Sheet {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            if any(c is not None for c in row):
                parts.append(" | ".join("" if c is None else str(c) for c in row))
    return "\n".join(parts)


def call_tool(*, system, content, tool_name, description, schema):
    """Ask the model and return the arguments of its forced call to ``tool_name`` (a dict)."""
    client = _client()
    import anthropic

    # Current models don't allow forcing a particular tool, so the instruction asks for it and the answer is
    # checked; one reminder is sent if the model replied in prose instead.
    messages = [{"role": "user", "content": content}]
    system = f"{system}\n\nAlways answer by calling the {tool_name} tool exactly once. Don't reply in prose."
    for _ in range(2):
        try:
            message = client.messages.create(
                model=settings.ILA_LLM_MODEL, max_tokens=MAX_TOKENS, system=system,
                tools=[{"name": tool_name, "description": description, "input_schema": schema}],
                tool_choice={"type": "auto"}, messages=messages,
            )
        except anthropic.APIError as exc:
            # Never log or show str(exc): a malformed key can appear in it.
            logger.warning("Language model call failed: %s %s", type(exc).__name__, getattr(exc, "status_code", ""))
            raise LLMError(_explain(exc, anthropic)) from None
        for block in message.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        if message.stop_reason == "max_tokens":
            break
        messages = messages + [{"role": "assistant", "content": message.content},
                               {"role": "user", "content": f"Please give your answer by calling {tool_name}."}]
    raise LLMError("The language model didn't return a usable answer. Try again, or with fewer files at once.")


def _explain(exc, anthropic):
    if isinstance(exc, anthropic.AuthenticationError):
        return "The language model rejected iLA's API key. Check ANTHROPIC_API_KEY in the server's settings."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The API key isn't allowed to use this model. Check the key's workspace or ILA_LLM_MODEL."
    if isinstance(exc, anthropic.NotFoundError):
        return f"The model {settings.ILA_LLM_MODEL} wasn't found. Check ILA_LLM_MODEL in the server's settings."
    if isinstance(exc, anthropic.RateLimitError):
        return "The language model is busy (rate limit). Try again in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        message = (getattr(exc, "body", None) or {}).get("error", {}).get("message", "") if isinstance(
            getattr(exc, "body", None), dict) else ""
        if "credit balance" in message.lower():
            return "The Anthropic account has no credit left. Add credit in the Anthropic console."
        return "The language model refused the request" + (f": {message[:200]}" if message else ".") + \
            " A very large or protected file can cause this; try the files one at a time."
    if isinstance(exc, anthropic.APIConnectionError):
        return ("Couldn't connect to the language model. Check the internet connection, and that ANTHROPIC_API_KEY "
                "has no spaces or quotes around it.")
    return "The language model had a problem with the request. Try again in a minute."
