"""Isolated, no-cookie, GET-only browser capture with explicit per-origin policy."""

import asyncio
import ipaddress
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.core.config import Settings

MAX_LINKS = 3
MAX_ATTEMPTS = 2
MAX_PAGE_TEXT_CHARS = 6000
MAX_RESPONSE_BYTES_EACH = 2 * 1024 * 1024
_SAFE_REQUEST_HEADERS = {"accept", "accept-language"}
_SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-security-policy",
    "x-content-type-options",
    "referrer-policy",
    "location",
}


class LegacyBrowserPolicyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CapturedPage:
    label: str
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class CaptureResult:
    pages: list[CapturedPage]
    request_count: int
    response_bytes: int


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LegacyBrowserPolicyError("invalid_origin_configuration")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        raise LegacyBrowserPolicyError("invalid_origin_configuration") from None
    if not host or (port is not None and not 1 <= port <= 65535):
        raise LegacyBrowserPolicyError("invalid_origin_configuration")
    host_part = f"[{host}]" if ":" in host else host
    port_part = f":{port}" if port is not None and not (parsed.scheme == "https" and port == 443) and not (
        parsed.scheme == "http" and port == 80
    ) else ""
    return f"{parsed.scheme}://{host_part}{port_part}"


def allowed_origins(settings: Settings) -> frozenset[str]:
    values = [item.strip() for item in settings.rag_legacy_browser_allowed_origins.split(",") if item.strip()]
    if not values:
        return frozenset()
    return frozenset(_canonical_origin(item) for item in values)


def _origin_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise LegacyBrowserPolicyError("invalid_url")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        raise LegacyBrowserPolicyError("invalid_url") from None
    host_part = f"[{host}]" if ":" in host else host
    port_part = f":{port}" if port is not None and not (parsed.scheme == "https" and port == 443) and not (
        parsed.scheme == "http" and port == 80
    ) else ""
    return f"{parsed.scheme}://{host_part}{port_part}"


def validate_target_url(url: str, settings: Settings, *, navigation: bool = True) -> str:
    try:
        parsed = urlsplit(url.strip())
        origin = _origin_for_url(url.strip())
    except ValueError:
        raise LegacyBrowserPolicyError("invalid_url") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or "\\" in parsed.path
        or any(ord(char) < 0x20 for char in url)
        or (navigation and (parsed.query or parsed.fragment))
    ):
        raise LegacyBrowserPolicyError("invalid_url")
    if origin not in allowed_origins(settings):
        raise LegacyBrowserPolicyError("origin_not_allowed")
    if parsed.scheme == "http":
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback and not settings.rag_legacy_browser_allow_private_networks:
            raise LegacyBrowserPolicyError("https_required")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "" if navigation else parsed.query, ""))


def origin_for_target(url: str) -> str:
    return _origin_for_url(url)


def ensure_available(settings: Settings) -> Path:
    if not settings.rag_legacy_browser_enabled or not allowed_origins(settings):
        raise LegacyBrowserPolicyError("feature_disabled")
    executable = settings.rag_legacy_browser_executable_path
    if executable is None or not executable.is_absolute() or not executable.is_file():
        raise LegacyBrowserPolicyError("browser_not_configured")
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        raise LegacyBrowserPolicyError("browser_dependency_missing") from None
    return executable


async def _assert_network_target(host: str, settings: Settings) -> None:
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            resolved = await asyncio.wait_for(
                loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=2.0
            )
        except (OSError, asyncio.TimeoutError):
            raise LegacyBrowserPolicyError("host_resolution_failed") from None
        addresses = []
        for item in resolved:
            try:
                address = ipaddress.ip_address(item[4][0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise LegacyBrowserPolicyError("host_resolution_failed")
    for address in addresses:
        if address.is_loopback:
            continue
        if address.is_multicast or address.is_unspecified or address.is_link_local or address.is_reserved:
            raise LegacyBrowserPolicyError("private_network_blocked")
        if address.is_private and not settings.rag_legacy_browser_allow_private_networks:
            raise LegacyBrowserPolicyError("private_network_blocked")


async def capture_readonly(
    settings: Settings, start_url: str, link_labels: list[str]
) -> CaptureResult:
    """Navigate and extract bounded visible text without inherited cookies or persistent state."""
    executable = ensure_available(settings)
    start_url = validate_target_url(start_url, settings)
    if len(link_labels) > MAX_LINKS:
        raise LegacyBrowserPolicyError("action_budget_exceeded")

    from playwright.async_api import async_playwright
    import httpx

    max_requests = settings.rag_legacy_browser_max_requests
    max_bytes = settings.rag_legacy_browser_max_response_bytes
    deadline = asyncio.get_running_loop().time() + settings.rag_legacy_browser_timeout_seconds
    request_count = 0
    response_bytes = 0
    result_chars = 0
    captured: list[CapturedPage] = []
    route_policy_errors: list[str] = []
    browser = None

    async def bounded_fetch(url: str, request_headers: dict[str, str]):
        nonlocal response_bytes
        safe_url = validate_target_url(url, settings, navigation=False)
        parsed = urlsplit(safe_url)
        await _assert_network_target(parsed.hostname or "", settings)
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        remaining_bytes = max_bytes - response_bytes
        if remaining_seconds <= 0 or remaining_bytes <= 0:
            raise LegacyBrowserPolicyError("run_budget_exceeded")
        headers = {key: value for key, value in request_headers.items() if key.lower() in _SAFE_REQUEST_HEADERS}
        timeout = httpx.Timeout(min(remaining_seconds, 10.0), connect=min(remaining_seconds, 4.0))
        body = bytearray()
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, trust_env=False) as client:
            async with client.stream("GET", safe_url, headers=headers) as response:
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > min(
                    remaining_bytes, MAX_RESPONSE_BYTES_EACH
                ):
                    raise LegacyBrowserPolicyError("response_too_large")
                content_type = response.headers.get("content-type", "").lower()
                disposition = response.headers.get("content-disposition", "").lower()
                if "attachment" in disposition or content_type.startswith(("application/octet-stream", "application/x-msdownload")):
                    raise LegacyBrowserPolicyError("downloads_blocked")
                if response.status_code not in {301, 302, 303, 307, 308}:
                    async for block in response.aiter_bytes():
                        if len(body) + len(block) > min(remaining_bytes, MAX_RESPONSE_BYTES_EACH):
                            raise LegacyBrowserPolicyError("response_too_large")
                        body.extend(block)
                response_headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in _SAFE_RESPONSE_HEADERS
                }
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise LegacyBrowserPolicyError("invalid_redirect")
                    next_url = validate_target_url(urljoin(safe_url, location), settings)
                    response_headers["location"] = next_url
                else:
                    response_bytes += len(body)
                    response_headers["content-length"] = str(len(body))
                return response.status_code, response_headers, bytes(body)

    async def run_browser() -> CaptureResult:
        nonlocal browser, result_chars
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=str(executable), headless=True,
                timeout=settings.rag_legacy_browser_timeout_seconds * 1000,
            )
            context = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                ignore_https_errors=False,
                java_script_enabled=True,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            context.on("page", lambda opened: asyncio.create_task(opened.close()) if opened != page else None)

            async def route_request(route):
                nonlocal request_count
                request = route.request
                request_count += 1
                try:
                    if request_count > max_requests:
                        raise LegacyBrowserPolicyError("request_budget_exceeded")
                    if request.method != "GET":
                        route_policy_errors.append("write_method_blocked")
                        await route.abort("blockedbyclient")
                        return
                    validate_target_url(request.url, settings, navigation=request.is_navigation_request())
                    status_code, headers, body = await bounded_fetch(request.url, request.headers)
                    await route.fulfill(status=status_code, headers=headers, body=body)
                except LegacyBrowserPolicyError as exc:
                    route_policy_errors.append(exc.code)
                    await route.abort("blockedbyclient")
                except Exception:
                    await route.abort("failed")

            await context.route("**/*", route_request)

            async def capture(label: str) -> None:
                nonlocal result_chars
                if asyncio.get_running_loop().time() >= deadline:
                    raise LegacyBrowserPolicyError("run_budget_exceeded")
                await page.wait_for_load_state("domcontentloaded", timeout=max(
                    1000, min(settings.rag_legacy_browser_timeout_seconds * 1000,
                              int((deadline - asyncio.get_running_loop().time()) * 1000))
                ))
                await page.wait_for_timeout(100)
                if any(code in {"request_budget_exceeded", "response_too_large"} for code in route_policy_errors):
                    raise LegacyBrowserPolicyError(route_policy_errors[-1])
                page_url = validate_target_url(page.url, settings)
                title = (await page.title())[:300]
                remaining = min(MAX_PAGE_TEXT_CHARS, settings.rag_legacy_browser_max_result_chars - result_chars)
                if remaining <= 0:
                    raise LegacyBrowserPolicyError("result_budget_exceeded")
                body_text = await page.locator("body").inner_text(timeout=3000)
                page_text = body_text[:remaining]
                result_chars += len(page_text)
                captured.append(CapturedPage(label=label, url=page_url, title=title, text=page_text))

            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=settings.rag_legacy_browser_timeout_seconds * 1000)
            except Exception:
                if route_policy_errors:
                    raise LegacyBrowserPolicyError(route_policy_errors[-1]) from None
                raise
            await capture("Start page")
            for link_label in link_labels:
                if asyncio.get_running_loop().time() >= deadline:
                    raise LegacyBrowserPolicyError("run_budget_exceeded")
                locator = page.get_by_role("link", name=link_label, exact=True)
                if await locator.count() != 1:
                    raise LegacyBrowserPolicyError("link_not_unique")
                raw_href = await locator.get_attribute("href")
                if not raw_href:
                    raise LegacyBrowserPolicyError("link_unavailable")
                target = validate_target_url(urljoin(page.url, raw_href), settings)
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=settings.rag_legacy_browser_timeout_seconds * 1000)
                except Exception:
                    if route_policy_errors:
                        raise LegacyBrowserPolicyError(route_policy_errors[-1]) from None
                    raise
                await capture(link_label)
            await context.close()
            await browser.close()
            return CaptureResult(captured, request_count, response_bytes)

    try:
        return await asyncio.wait_for(run_browser(), timeout=settings.rag_legacy_browser_timeout_seconds + 3)
    except asyncio.TimeoutError:
        raise LegacyBrowserPolicyError("run_budget_exceeded") from None
    except LegacyBrowserPolicyError:
        raise
    except Exception as exc:
        if "Timeout" in type(exc).__name__:
            raise LegacyBrowserPolicyError("run_budget_exceeded") from None
        raise LegacyBrowserPolicyError("browser_capture_failed") from None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
