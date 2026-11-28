"""WhatsApp 24-hour window templates (Step F slice 6).

Meta's Cloud API only allows free-form text inside the 24-hour
customer-service window that opens with the user's last message. Outside
that window only pre-approved template messages may be sent — a template
has fixed, approved text plus a small number of parameters.

This module is the catalogue for those templates: pure validation and
rendering, no network. The actual send lives in
``app.integrations.whatsapp.send_template`` (Cloud) with the Baileys leg
sending the rendered text (a normal WhatsApp account has no 24-hour
window, so the same words go out as a free-form message there).

Templates are code, not configuration: adding a template means adding a
catalogue entry here (and getting it approved in Meta's dashboard) — a
message can never invent a template name at runtime.
"""

from __future__ import annotations

import re
from typing import Any

# Meta template names are lowercase letters, digits and underscores.
_TEMPLATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
# Language codes look like "en_US" (Meta's format).
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")

MAX_TEMPLATE_PARAMETER_CHARS = 1024
MAX_TEMPLATE_SUMMARY_CHARS = 200

# The invitation parameter when nothing was computed yet (the window
# expired before the agent ran): the user just needs to reply.
TEMPLATE_INVITATION = "Reply here to continue."

# The one template this slice ships. Utility category, one parameter: a
# short summary of what is waiting for the user.
TASK_READY_TEMPLATE = "task_ready"
TASK_READY_LANGUAGE = "en_US"

TEMPLATES: dict[str, dict[str, Any]] = {
    TASK_READY_TEMPLATE: {
        "language": TASK_READY_LANGUAGE,
        "category": "utility",
        "parameters": 1,
        # {{1}} is filled with :func:`template_summary` output.
        "body": "Your InfraSpine AI task is ready. {{1}}",
    },
}


def validate_template(name: str, language: str, parameters: list[Any]) -> str:
    """Validate a template send; returns the template name.

    Raises :class:`ValueError` on an unknown template, a malformed name or
    language, a language the template was not approved in, a wrong
    parameter count, or a parameter that is not a short string. The check
    runs before any network call, so a bad template can never reach Meta.
    """
    if not isinstance(name, str) or _TEMPLATE_NAME_RE.match(name) is None:
        raise ValueError(f"invalid WhatsApp template name: {name!r}")
    template = TEMPLATES.get(name)
    if template is None:
        raise ValueError(f"unknown WhatsApp template: {name!r}")
    if not isinstance(language, str) or _LANGUAGE_RE.match(language) is None:
        raise ValueError(f"invalid WhatsApp template language: {language!r}")
    if language != template["language"]:
        raise ValueError(
            f"template {name!r} is approved in {template['language']!r}, not {language!r}"
        )
    if not isinstance(parameters, list) or len(parameters) != template["parameters"]:
        raise ValueError(
            f"template {name!r} takes {template['parameters']} parameter(s), "
            f"got {len(parameters) if isinstance(parameters, list) else 'not a list'}"
        )
    for parameter in parameters:
        if not isinstance(parameter, str) or not parameter.strip():
            raise ValueError(f"template {name!r} parameters must be non-empty strings")
        if len(parameter) > MAX_TEMPLATE_PARAMETER_CHARS:
            raise ValueError(
                f"template {name!r} parameter exceeds {MAX_TEMPLATE_PARAMETER_CHARS} characters"
            )
    return name


def render_template_text(name: str, parameters: list[str]) -> str:
    """The human-readable text of a template send (Baileys leg, audit log).

    The Cloud leg sends the structured template; this is only the plain
    text form for transports without templates.
    """
    template = TEMPLATES.get(name)
    if template is None:
        raise ValueError(f"unknown WhatsApp template: {name!r}")
    validate_template(name, template["language"], parameters)
    body = template["body"]
    for index, parameter in enumerate(parameters, start=1):
        body = body.replace("{{" + str(index) + "}}", parameter)
    return body


def template_summary(reply: str | None) -> str:
    """The one parameter of the task-ready template: a short summary.

    The reply's first line, whitespace-collapsed and capped — never the
    whole reply (a template parameter is not a place for arbitrary text).
    An empty reply becomes the generic invitation to reply.
    """
    if not isinstance(reply, str):
        return TEMPLATE_INVITATION
    summary = " ".join(reply.split())[:MAX_TEMPLATE_SUMMARY_CHARS].strip()
    return summary or TEMPLATE_INVITATION
