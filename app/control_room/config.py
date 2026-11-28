"""Step AF slice 5: per-scope alert configuration (pure functions, no I/O).

Rules are data, not code. A scope's stored config (``control_room_alert_configs``
row, or nothing when no admin has saved one) is merged over the slice-1
``DEFAULT_RULES`` by :func:`effective_rules`: any rule key the admin did not
touch keeps its default threshold, cooldown, severity and quiet-hour
behaviour. Only the listed overrides change.

Validation is strict and fail-closed: an unknown rule key, a negative
threshold, a non-positive cooldown, an unknown channel, a malformed
recipient or an unreadable quiet-hour time is a problem, never silently
ignored. Recipients are the ONLY addresses alerts are ever sent to — a
phone number or email is never taken from document or message text.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Any

from app.control_room.alerts import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DEFAULT_RULES,
    AlertRule,
)

CHANNELS = ("whatsapp", "email")
# E.164-ish: optional leading +, 7-15 digits. WhatsApp Cloud API needs E.164.
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Override keys an admin may set per rule. Severity is NOT configurable:
# the evaluator's critical/warning split is part of the rule's meaning.
RULE_OVERRIDE_KEYS = {"enabled", "threshold", "cooldown_seconds", "breaks_quiet_hours", "channels"}


def validate_recipient(recipient) -> list[str]:
    """Problems with one ``{"channel": ..., "address": ...}`` recipient."""
    problems: list[str] = []
    if not isinstance(recipient, dict):
        return ["recipient must be an object with channel and address"]
    channel = recipient.get("channel")
    address = recipient.get("address")
    if channel not in CHANNELS:
        problems.append(f"recipient channel must be one of {list(CHANNELS)}")
        return problems
    if not isinstance(address, str) or not address.strip():
        problems.append(f"recipient address for {channel} must be a non-empty string")
        return problems
    address = address.strip()
    if channel == "whatsapp" and not _PHONE_RE.fullmatch(address):
        problems.append(f"whatsapp recipient must be an E.164 phone number, got {address!r}")
    if channel == "email" and not _EMAIL_RE.fullmatch(address):
        problems.append(f"email recipient must be an email address, got {address!r}")
    return problems


def _parse_quiet(value, field: str) -> tuple[time | None, list[str]]:
    """Accept a ``time`` or an ``"HH:MM"`` string; anything else is a problem."""
    if isinstance(value, time):
        return value, []
    if isinstance(value, str):
        try:
            hour, minute = value.strip().split(":")
            parsed = time(int(hour), int(minute))
            return parsed, []
        except (ValueError, TypeError):
            return None, [f"{field} must be an HH:MM time"]
    return None, [f"{field} must be an HH:MM time"]


def validate_config_payload(payload) -> list[str]:
    """Problems with a config payload (empty means valid).

    The payload is what the admin API accepts: ``enabled``, ``quiet_start``,
    ``quiet_end``, ``recipients`` (list) and ``rules`` (rule key -> overrides).
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["config must be an object"]
    for field in payload:
        if field not in {"enabled", "quiet_start", "quiet_end", "recipients", "rules"}:
            problems.append(f"unknown config field {field!r}")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        problems.append("enabled must be a boolean")
    # Quiet hours are validated on their effective values (a missing side
    # falls back to the default): each side must parse, and the window
    # must not be empty — start == end would silently mean "never quiet".
    start_raw = payload.get("quiet_start", DEFAULT_QUIET_START.strftime("%H:%M"))
    end_raw = payload.get("quiet_end", DEFAULT_QUIET_END.strftime("%H:%M"))
    start, start_errs = _parse_quiet(start_raw, "quiet_start")
    end, end_errs = _parse_quiet(end_raw, "quiet_end")
    problems.extend(start_errs)
    problems.extend(end_errs)
    if not start_errs and not end_errs and start == end:
        problems.append("quiet_start and quiet_end must differ")
    recipients = payload.get("recipients", [])
    if not isinstance(recipients, list):
        problems.append("recipients must be a list")
    else:
        if len(recipients) > 10:
            problems.append("at most 10 recipients")
        for recipient in recipients:
            problems.extend(validate_recipient(recipient))
    rules = payload.get("rules", {})
    if not isinstance(rules, dict):
        problems.append("rules must be an object keyed by rule key")
    else:
        known = {rule.key for rule in DEFAULT_RULES}
        for key, overrides in rules.items():
            if key not in known:
                problems.append(f"unknown rule key: {key}")
                continue
            if not isinstance(overrides, dict):
                problems.append(f"rule {key}: overrides must be an object")
                continue
            for field in overrides:
                if field not in RULE_OVERRIDE_KEYS:
                    problems.append(f"rule {key}: unknown override field {field!r}")
            if "enabled" in overrides and not isinstance(overrides["enabled"], bool):
                problems.append(f"rule {key}: enabled must be a boolean")
            if "threshold" in overrides:
                threshold = overrides["threshold"]
                if isinstance(threshold, bool) or not isinstance(threshold, int):
                    problems.append(f"rule {key}: threshold must be an integer")
                elif threshold <= 0:
                    problems.append(f"rule {key}: threshold must be positive")
            if "cooldown_seconds" in overrides:
                cooldown = overrides["cooldown_seconds"]
                if isinstance(cooldown, bool) or not isinstance(cooldown, int):
                    problems.append(f"rule {key}: cooldown_seconds must be an integer")
                elif cooldown <= 0:
                    problems.append(f"rule {key}: cooldown_seconds must be positive")
            if "breaks_quiet_hours" in overrides and not isinstance(overrides["breaks_quiet_hours"], bool):
                problems.append(f"rule {key}: breaks_quiet_hours must be a boolean")
            if "channels" in overrides:
                channels = overrides["channels"]
                if not isinstance(channels, list) or not channels:
                    problems.append(f"rule {key}: channels must be a non-empty list")
                else:
                    for channel in channels:
                        if channel not in (*CHANNELS, "in_app"):
                            problems.append(f"rule {key}: unknown channel {channel!r}")
    return problems


def effective_rules_list(config: dict | None) -> list[dict[str, Any]]:
    """Merged rule catalogue for the admin UI: one row per rule, defaults included.

    The UI shows the current value next to the default and only sends an
    override for a rule whose values differ from the default (see
    ``frontend/src/control-room/controlRoomConfig.mjs``).
    """
    merged = {rule.key: rule for rule in effective_rules(config)}
    rows: list[dict[str, Any]] = []
    for rule in DEFAULT_RULES:
        current = merged[rule.key]
        rows.append(
            {
                "key": rule.key,
                "title": rule.title,
                "severity": rule.severity,
                "threshold": current.threshold,
                "cooldown_seconds": current.cooldown_seconds,
                "enabled": current.enabled,
                "breaks_quiet_hours": current.breaks_quiet_hours,
                "channels": rule_channels(config, rule.key),
                "default_threshold": rule.threshold,
                "default_cooldown_seconds": rule.cooldown_seconds,
                "default_enabled": rule.enabled,
                "default_breaks_quiet_hours": rule.breaks_quiet_hours,
                "default_channels": ["whatsapp", "email", "in_app"],
            }
        )
    return rows


def effective_rules(config: dict | None, defaults=DEFAULT_RULES) -> tuple[AlertRule, ...]:
    """Merge a stored config over the defaults; a missing config = defaults."""
    if not config:
        return defaults
    overrides = config.get("rules") or {}
    merged: list[AlertRule] = []
    for rule in defaults:
        patch = overrides.get(rule.key)
        if not isinstance(patch, dict):
            merged.append(rule)
            continue
        merged.append(
            AlertRule(
                key=rule.key,
                severity=rule.severity,
                title=rule.title,
                detail=rule.detail,
                threshold=patch.get("threshold", rule.threshold),
                cooldown_seconds=patch.get("cooldown_seconds", rule.cooldown_seconds),
                breaks_quiet_hours=patch.get("breaks_quiet_hours", rule.breaks_quiet_hours),
                enabled=patch.get("enabled", rule.enabled),
            )
        )
    return tuple(merged)


def rule_channels(config: dict | None, rule_key: str) -> list[str]:
    """Channels a fired rule is sent on (per-rule override, else all configured)."""
    overrides = (config or {}).get("rules") or {}
    patch = overrides.get(rule_key) or {}
    channels = patch.get("channels")
    if isinstance(channels, list) and channels:
        return list(channels)
    return ["whatsapp", "email", "in_app"]


def config_defaults() -> dict:
    """The config a scope has before any admin saves one."""
    return {
        "enabled": True,
        "quiet_start": DEFAULT_QUIET_START.strftime("%H:%M"),
        "quiet_end": DEFAULT_QUIET_END.strftime("%H:%M"),
        "recipients": [],
        "rules": {},
    }


def config_to_dict(row) -> dict:
    """Serialise a stored config row (or the defaults when there is none)."""
    if row is None:
        return config_defaults()
    return {
        "enabled": bool(row.enabled),
        "quiet_start": row.quiet_start.strftime("%H:%M"),
        "quiet_end": row.quiet_end.strftime("%H:%M"),
        "recipients": list(row.recipients or []),
        "rules": dict(row.rules or {}),
    }


def parse_quiet(value, field: str) -> tuple[time | None, list[str]]:
    """Public wrapper for :func:`_parse_quiet` (route layer)."""
    return _parse_quiet(value, field)
