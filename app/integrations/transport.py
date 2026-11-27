"""Bounded, DNS-pinned HTTPS requests. No redirects, proxies or response-body logs."""
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.providers.endpoint_security import EndpointPolicy, guarded_http_client, normalize_base_url, check_endpoint

MAX_RESPONSE = 2_000_000


class TransportError(Exception):
    def __init__(self, code, *, retryable=False, ambiguous=False, retry_after=0):
        super().__init__(code)
        self.code, self.retryable, self.ambiguous = code, retryable, ambiguous
        self.retry_after = min(900, max(0, retry_after))


@dataclass
class Response:
    status: int
    body: dict | list | str


def retry_seconds(value):
    try:
        return min(900, max(1, int(value)))
    except (ValueError, TypeError):
        try:
            return min(900, max(1, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())))
        except (ValueError, TypeError, OverflowError):
            return 30


async def request(base_url, path, *, method="GET", token="", auth_header="Authorization", payload=None, params=None, form=None, files=None, xml=None, write=False):
    endpoint = normalize_base_url(base_url)
    policy = EndpointPolicy()
    check_endpoint(endpoint, policy)
    headers = {"Accept": "application/json", "User-Agent": "InfraSpine-Integrations/1"}
    if token:
        headers[auth_header] = f"Bearer {token}" if auth_header == "Authorization" else token
    if xml is not None:
        headers["Content-Type"] = "application/soap+xml; charset=utf-8"
    try:
        async with asyncio.timeout(20):
            async with guarded_http_client(endpoint, policy) as client:
                async with client.stream(method, base_url.rstrip("/") + path, headers=headers, params=params,
                                         json=payload, data=form, files=files, content=xml, timeout=15) as response:
                    status = response.status_code
                    if status == 429:
                        raise TransportError("rate_limited", retryable=True, retry_after=retry_seconds(response.headers.get("Retry-After")))
                    if status in (401, 403):
                        raise TransportError("credentials_or_scope_rejected")
                    if status >= 500:
                        raise TransportError("provider_unavailable", retryable=not write, ambiguous=write)
                    if status < 200 or status >= 300:
                        raise TransportError("provider_rejected")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise TransportError("response_too_large", ambiguous=write)
                        raw.extend(chunk)
                    if xml is not None:
                        from defusedxml.ElementTree import fromstring
                        try:
                            fromstring(bytes(raw))
                            body = raw.decode("utf-8")
                        except Exception:
                            raise TransportError("invalid_xml_response", ambiguous=write) from None
                    elif raw:
                        try:
                            body = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            raise TransportError("invalid_provider_response", ambiguous=write) from None
                    else:
                        body = {}
                    return Response(status, body)
    except TransportError:
        raise
    except Exception:
        # Transport exceptions may embed credential headers or response bodies.
        raise TransportError("transport_unavailable", retryable=not write, ambiguous=write) from None
