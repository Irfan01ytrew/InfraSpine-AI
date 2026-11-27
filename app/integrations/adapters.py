"""Supported provider wire formats and declarative normalization."""
import base64
import re
from email.message import EmailMessage
from urllib.parse import urlsplit, parse_qsl

from app.core.errors import InvalidInputError
from app.integrations import transport
from app.integrations.catalog import CATALOG, WRITE_SCOPES
from app.integrations.schemas import CalendarEvent, ConnectorConfig, Email, SlackMessage


def value_at(data, path, default=None):
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def normalize_record(item, config):
    external = value_at(item, config.id_field)
    if not isinstance(external, (str, int)) or isinstance(external, bool) or not 1 <= len(str(external)) <= 240:
        raise TransportError("invalid_record_identity")
    mapped = {} if config.mappings else item
    for rule in config.mappings:
        value = value_at(item, rule.source)
        if value is None:
            if rule.required:
                raise TransportError("required_field_missing")
            mapped[rule.target] = None
            continue
        try:
            if rule.transform != "identity" and not isinstance(value, (str, int, float, bool)):
                raise ValueError()
            if rule.transform == "string": value = str(value)
            elif rule.transform == "integer":
                if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)): raise ValueError()
                value = int(value)
            elif rule.transform == "lowercase": value = str(value).lower()
            elif rule.transform == "uppercase": value = str(value).upper()
            elif rule.transform == "boolean":
                if str(value).lower() not in ("true", "false", "1", "0"): raise ValueError()
                value = str(value).lower() in ("true", "1")
        except (TypeError, ValueError, OverflowError):
            raise TransportError("mapping_validation_failed") from None
        mapped[rule.target] = value
    return str(external), mapped


TransportError = transport.TransportError


def validate_operation(connector, operation, payload):
    if operation not in CATALOG[connector.provider]["operations"]:
        raise InvalidInputError("This connector does not support the requested operation.")
    config = ConnectorConfig.model_validate(connector.config)
    required = WRITE_SCOPES.get((connector.provider, operation))
    if required and required not in config.scopes:
        raise InvalidInputError("Configure the provider scope required for this operation.")
    if operation in ("verify", "sync"):
        if payload:
            raise InvalidInputError("Read operations take no action payload.")
        if connector.provider == "graphql":
            query = config.graphql_query.strip()
            if not query or not (query.startswith("query ") or query.startswith("query{") or query.startswith("{")) or re.search(r"\b(mutation|subscription)\b", query, re.I):
                raise InvalidInputError("Configure a read-only GraphQL query.")
    if operation == "email.send": return Email.model_validate(payload).model_dump(mode="json")
    if operation == "calendar.create": return CalendarEvent.model_validate(payload).model_dump(mode="json")
    if operation == "slack.send": return SlackMessage.model_validate(payload).model_dump(mode="json")
    if operation == "soap.call":
        from defusedxml.ElementTree import fromstring
        if set(payload) != {"xml"} or not isinstance(payload["xml"], str) or len(payload["xml"]) > 100000:
            raise InvalidInputError("Supply a bounded SOAP envelope in xml.")
        try:
            root = fromstring(payload["xml"])
            if root.tag not in ("{http://schemas.xmlsoap.org/soap/envelope/}Envelope", "{http://www.w3.org/2003/05/soap-envelope}Envelope"):
                raise ValueError()
        except Exception:
            raise InvalidInputError("Invalid or unsafe SOAP envelope.") from None
    return payload


async def call(connector, secret, path, **kwargs):
    response = await transport.request(connector.base_url, path, token=secret.get("access_token", ""),
                                       auth_header=connector.config["auth_header"], **kwargs)
    if connector.provider == "slack" and (not isinstance(response.body, dict) or response.body.get("ok") is not True):
        raise TransportError("provider_rejected")
    if connector.provider == "graphql" and (not isinstance(response.body, dict) or response.body.get("errors")):
        raise TransportError("graphql_rejected")
    return response


async def verify(connector, secret):
    config = ConnectorConfig.model_validate(connector.config)
    path = config.verify_path
    kwargs = {}
    if connector.provider == "google_drive": kwargs["params"] = {"fields": "user(permissionId)"}
    if connector.provider == "graphql":
        kwargs.update(method="POST", payload={"query": config.graphql_query})
    response = await call(connector, secret, path, **kwargs)
    return {"provider_status": response.status, "verified": True}


async def fetch_records(connector, secret):
    config = ConnectorConfig.model_validate(connector.config)
    cursor = connector.cursor
    path, params = config.sync_path, {}
    records, seen = {}, set()
    final_cursor = None
    for page in range(config.max_pages):
        if cursor:
            if cursor in seen: raise TransportError("pagination_cycle")
            seen.add(cursor)
            if connector.provider == "microsoft365":
                base = urlsplit(connector.base_url); link = urlsplit(cursor)
                if link.scheme != base.scheme or link.netloc != base.netloc or not link.path.startswith(base.path + "/"):
                    raise TransportError("unsafe_pagination_link")
                path, params = link.path[len(base.path):], dict(parse_qsl(link.query))
            else:
                params = {"pageToken" if connector.provider.startswith("google_") or connector.provider == "gmail" else config.cursor_parameter: cursor}
        kwargs = {"params": params}
        if connector.provider == "graphql":
            kwargs = {"method": "POST", "payload": {"query": config.graphql_query, "variables": {"cursor": cursor}}}
        response = await call(connector, secret, path, **kwargs)
        body = response.body
        rows = value_at(body, config.records_path)
        if not isinstance(rows, list): raise TransportError("invalid_record_collection")
        for row in rows:
            external_id, mapped = normalize_record(row, config)
            if external_id in records and records[external_id] != mapped:
                raise TransportError("duplicate_record_conflict")
            records[external_id] = mapped
            if len(records) > 1000: raise TransportError("record_budget_exceeded")
        if connector.provider == "microsoft365":
            cursor = body.get("@odata.nextLink")
            final_cursor = body.get("@odata.deltaLink")
        elif connector.provider.startswith("google_") or connector.provider == "gmail":
            cursor = body.get("nextPageToken")
        else:
            cursor = value_at(body, config.next_cursor_path)
            final_cursor = value_at(body, "sync_cursor")
        if not cursor:
            if final_cursor is not None and (not isinstance(final_cursor, str) or len(final_cursor) > 2048):
                raise TransportError("invalid_sync_cursor")
            return records, final_cursor, page + 1
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise TransportError("invalid_page_cursor")
    raise TransportError("page_budget_exceeded")


async def execute_write(connector, secret, job, payload):
    config = ConnectorConfig.model_validate(connector.config)
    path, body, xml = config.write_path, payload, None
    if job.operation == "soap.call": xml, body = payload["xml"], None
    elif job.operation == "email.send":
        email = Email.model_validate(payload)
        if connector.provider == "microsoft365":
            path = "/me/sendMail"
            body = {"message": {"subject": email.subject, "body": {"contentType": "Text", "content": email.text},
                                "toRecipients": [{"emailAddress": {"address": x}} for x in email.to],
                                "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": a.name,
                                                 "contentType": a.content_type, "contentBytes": a.content_base64} for a in email.attachments]},
                    "saveToSentItems": True}
        else:
            message = EmailMessage(); message["To"] = ", ".join(email.to); message["Subject"] = email.subject
            message.set_content(email.text)
            for attachment in email.attachments:
                main, sub = attachment.content_type.split("/")
                message.add_attachment(base64.b64decode(attachment.content_base64), maintype=main, subtype=sub, filename=attachment.name)
            path, body = "/users/me/messages/send", {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    elif job.operation == "calendar.create":
        event = CalendarEvent.model_validate(payload)
        if connector.provider == "microsoft365":
            path = "/me/events"
            body = {"subject": event.title, "transactionId": str(job.id),
                    "start": {"dateTime": event.start.isoformat(), "timeZone": "UTC"},
                    "end": {"dateTime": event.end.isoformat(), "timeZone": "UTC"},
                    "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in event.attendees]}
        else:
            path = "/calendars/primary/events"
            body = {"id": job.id.hex, "summary": event.title, "start": {"dateTime": event.start.isoformat()},
                    "end": {"dateTime": event.end.isoformat()}, "attendees": [{"email": a} for a in event.attendees]}
    elif job.operation == "slack.send":
        path = "/chat.postMessage"; body = {**payload, "client_msg_id": str(job.id)}
    response = await call(connector, secret, path, method="POST", payload=body, xml=xml, write=True)
    provider_id = response.body.get("id") or response.body.get("ts") if isinstance(response.body, dict) else None
    if job.operation == "calendar.create" and not provider_id:
        raise TransportError("missing_event_identifier", ambiguous=True)
    if job.operation == "email.send" and connector.provider == "gmail" and not provider_id:
        raise TransportError("missing_message_identifier", ambiguous=True)
    if job.operation == "email.send" and connector.provider == "microsoft365" and response.status != 202:
        raise TransportError("unexpected_mail_response", ambiguous=True)
    return {"provider_status": response.status, "provider_id": str(provider_id)[:240] if provider_id else None,
            "acceptance": "provider_accepted", "delivery_verified": False}
