"""Classification, untrusted-data redaction, state transitions and action binding."""
import hashlib
import json
import re
from datetime import UTC, datetime

from app.core.errors import ConflictError, InvalidInputError

TERMINAL = {"resolved", "cancelled"}
ACTIVE_COMMANDS = {"proposed", "awaiting_approval", "queued", "executing", "verifying"}
STATES = {"detected", "triaged", "investigating", "recovery_proposed", "awaiting_approval", "recovering", "verifying", "resolved", "failed", "escalated", "reopened", "cancelled"}
TRANSITIONS = {
    "detected": {"triaged", "investigating", "escalated", "cancelled", "resolved"},
    "reopened": {"triaged", "investigating", "escalated", "cancelled", "resolved"},
    "triaged": {"investigating", "escalated", "cancelled", "resolved"},
    "investigating": {"escalated", "cancelled", "resolved"},
    "recovery_proposed": {"escalated", "cancelled"},
    "awaiting_approval": {"escalated", "cancelled"},
    "recovering": {"escalated"},
    "verifying": {"escalated"},
    "failed": {"investigating", "escalated", "cancelled", "resolved"},
    "escalated": {"investigating", "cancelled", "resolved"},
}
RISK = {"probe": "low", "retry": "low", "resume": "low", "reprocess": "medium", "fallback": "medium", "restart": "high", "compensate": "high", "route_approver": "high"}
_SECRET_KEY = re.compile(r"password|secret|token|authorization|cookie|credential|api.?key|private.?key", re.I)
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+\S+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+|(?:postgres(?:ql)?|https?)://[^\s/@]+:[^\s/@]+@[^\s]+)")


def now():
    return datetime.now(UTC)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact(value, depth=0):
    if depth > 5:
        return "[depth limit]"
    if isinstance(value, dict):
        return {str(k)[:80]: "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [redact(x, depth + 1) for x in value[:50]]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[redacted]", value)[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:100]


def bounded_evidence(value):
    if len(canonical(value)) > 32768:
        raise InvalidInputError("Evidence is limited to 32 KiB. Attach a scoped reference for larger artifacts.")
    return redact(value)


def check_version(incident, version):
    if incident.version != version:
        raise ConflictError("Incident changed. Refresh before acting.")


def action_digest(command, target, playbook):
    return digest({"tenant": command.tenant_id, "incident": command.incident_id, "generation": command.generation,
                   "target": target.id, "owner": target.owner_id, "adapter": target.adapter, "resource": target.resource,
                   "playbook": playbook.id, "action": playbook.action, "risk": playbook.risk,
                   "preapproved": playbook.preapproved, "attempts": playbook.max_attempts,
                   "timeout": playbook.timeout_seconds, "backoff": playbook.backoff_seconds,
                   "samples": playbook.verify_samples, "requester": command.requester_id, "key": command.request_key})
