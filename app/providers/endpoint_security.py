"""Outbound safety for admin-configured (custom) provider endpoints.

A custom provider's API key is sent to a URL that an administrator typed in. These rules keep that
from becoming a way to leak the key or to reach internal systems (SSRF):

* URLs are normalized once (`normalize_base_url`); the canonical *origin* (scheme, host, port) is what
  stored credentials are cryptographically bound to (see `app.core.crypto.credential_aad`).
* Remote endpoints must use HTTPS. Loopback/private addresses, and plain HTTP, are allowed only for
  origins listed in the deployment policy (`PROVIDER_TRUSTED_PRIVATE_ORIGINS`). Link-local (cloud
  metadata), multicast, reserved and other special-purpose ranges are never allowed.
* Every TCP connection re-resolves the host, checks every returned address and connects to the checked
  address itself (TLS still verifies the hostname). DNS rebinding therefore cannot swap in an internal
  address between validation and connection.
* The HTTP client is bound to the configured origin: redirects are never followed, requests to any
  other origin are refused, and environment proxies/netrc are ignored.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore2
import httpx2

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_DEFAULT_PORTS = {"https": 443, "http": 80}
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Hosts made only of digits, hex and dots ("2130706433", "0x7f.1", "127.1") are legacy IPv4 spellings
# that resolvers interpret inconsistently. Only standard dotted-quad IPv4 literals are accepted.
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+))*$")

# Private ranges a deployment may explicitly trust (an on-premises model server, for example).
_TRUSTABLE_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
)
# Never reachable, even for a trusted origin: cloud metadata services live here.
_FORBIDDEN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4",
        "::/96", "fe80::/10", "fec0::/10", "ff00::/8",
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeEndpointError(httpcore2.ConnectError):
    """The destination is not allowed. A ConnectError, so the HTTP stack treats it as a failed connection.
    `message` is safe to show to an administrator (it never contains a key)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Endpoint:
    base_url: str  # canonical form, no trailing slash
    origin: str  # "scheme://host:port" with an explicit port; the credential binding
    scheme: str
    host: str  # ASCII, lowercase; IPv6 without brackets
    port: int
    ip: IPAddress | None  # set when the host is an IP literal


@dataclass(frozen=True)
class EndpointPolicy:
    trusted_origins: frozenset[str] = frozenset()
    private_only: bool = False

    def trusts(self, endpoint: Endpoint) -> bool:
        return endpoint.origin in self.trusted_origins


def parse_trusted_origins(raw: str) -> frozenset[str]:
    """Comma-separated origins from deployment configuration. Raises ValueError on an invalid entry."""
    origins = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        try:
            endpoint = normalize_base_url(item)
        except UnsafeEndpointError as exc:
            raise ValueError(f"Invalid trusted provider origin: {exc.message}") from None
        if endpoint.base_url != _display_origin(endpoint):
            raise ValueError("Trusted provider origins must be scheme://host[:port] without a path.")
        origins.add(endpoint.origin)
    return frozenset(origins)


def policy_from_settings(settings) -> EndpointPolicy:
    return EndpointPolicy(
        parse_trusted_origins(settings.provider_trusted_private_origins),
        private_only=bool(getattr(settings, "llm_local_only", False)),
    )


# ------------------------------------------------------------------ normalization


def _canonical_host(raw: str) -> tuple[str, IPAddress | None]:
    host = raw.rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
            raise UnsafeEndpointError("IPv6 zone identifiers are not allowed in API URLs.")
        return ip.compressed, ip
    if _NUMERIC_HOST.fullmatch(host):
        raise UnsafeEndpointError("Use a hostname or a standard IP address in the API URL.")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise UnsafeEndpointError("The API URL host name is not valid.") from None
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise UnsafeEndpointError("The API URL host name is not valid.")
    return ascii_host, None


def _netloc(scheme: str, host: str, port: int, ip: IPAddress | None) -> str:
    shown = f"[{host}]" if isinstance(ip, ipaddress.IPv6Address) else host
    return shown if port == _DEFAULT_PORTS[scheme] else f"{shown}:{port}"


def _display_origin(endpoint: Endpoint) -> str:
    return f"{endpoint.scheme}://{_netloc(endpoint.scheme, endpoint.host, endpoint.port, endpoint.ip)}"


def normalize_base_url(raw: str) -> Endpoint:
    """Syntax-level validation and canonicalization (no network access, no policy)."""
    value = raw.strip()
    if not value or any(ch.isspace() or not ch.isprintable() or ch == "\\" for ch in value):
        raise UnsafeEndpointError("Enter a valid API base URL.")
    if "?" in value or "#" in value:
        raise UnsafeEndpointError("Enter an API base URL without query parameters or fragments.")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise UnsafeEndpointError("Enter a valid API base URL.") from None
    if scheme not in _DEFAULT_PORTS or not parsed.netloc or not hostname:
        raise UnsafeEndpointError("Enter a valid http(s) API base URL.")
    if "@" in parsed.netloc:
        raise UnsafeEndpointError("Enter an API base URL without credentials.")
    host, ip = _canonical_host(hostname)
    if port == 0:
        raise UnsafeEndpointError("Enter a valid API base URL.")
    port = port or _DEFAULT_PORTS[scheme]
    segments = parsed.path.split("/")
    if any(s in (".", "..") or s.lower() in ("%2e", "%2e%2e", ".%2e", "%2e.") for s in segments):
        raise UnsafeEndpointError("The API URL path must not contain '.' or '..' segments.")
    path = parsed.path.rstrip("/")
    bracketed = f"[{host}]" if isinstance(ip, ipaddress.IPv6Address) else host
    return Endpoint(
        base_url=f"{scheme}://{_netloc(scheme, host, port, ip)}{path}",
        origin=f"{scheme}://{bracketed}:{port}",
        scheme=scheme,
        host=host,
        port=port,
        ip=ip,
    )


def endpoint_origin(base_url: str) -> str:
    """The credential-binding origin of a stored base URL."""
    return normalize_base_url(base_url).origin


# ------------------------------------------------------------------ address policy


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded = [ip.ipv4_mapped, ip.sixtofour]
    if ip.teredo:
        embedded.extend(ip.teredo)
    if ip in _NAT64:
        embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return [v4 for v4 in embedded if v4 is not None]


def check_address(ip: IPAddress, *, trusted: bool, scheme: str = "https") -> None:
    """Raise unless `ip` may be contacted. Plain HTTP is only ever allowed to trusted private addresses."""
    if scheme != "https" and ip.is_global:
        raise UnsafeEndpointError("Use HTTPS for API URLs that resolve to public addresses.")
    candidates: list[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        candidates.extend(_embedded_ipv4(ip))
    for address in candidates:
        if address.is_multicast or address.is_unspecified or any(address in n for n in _FORBIDDEN_NETWORKS):
            raise UnsafeEndpointError("The API URL points to a reserved or internal network address.")
        if address.is_global:
            continue
        if trusted and any(address in n for n in _TRUSTABLE_NETWORKS):
            continue
        raise UnsafeEndpointError(
            "The API URL points to a private or internal address. Ask the deployment administrator to add "
            "this origin to PROVIDER_TRUSTED_PRIVATE_ORIGINS if it is an approved internal endpoint."
        )


def check_endpoint(endpoint: Endpoint, policy: EndpointPolicy) -> None:
    """Policy checks that need no DNS: HTTPS for remote endpoints, IP literals, local names."""
    trusted = policy.trusts(endpoint)
    if endpoint.scheme != "https" and not trusted:
        raise UnsafeEndpointError(
            "Use HTTPS for the API URL. Plain HTTP is allowed only for trusted private endpoints configured "
            "by the deployment administrator."
        )
    if endpoint.ip is not None:
        check_address(endpoint.ip, trusted=trusted, scheme=endpoint.scheme)
    elif not trusted and (
        endpoint.host == "localhost" or endpoint.host.endswith((".localhost", ".local", ".internal"))
    ):
        raise UnsafeEndpointError("The API URL points to a local or internal host name.")


async def resolve_host(host: str, port: int) -> list[IPAddress]:
    """System DNS resolution. Tests replace this function."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]


async def resolve_checked(endpoint: Endpoint, policy: EndpointPolicy) -> list[IPAddress]:
    """The addresses to contact. An IP literal is checked as is; a host name is resolved now and every
    answer is checked (one unsafe answer rejects the whole lookup)."""
    trusted = policy.trusts(endpoint)
    if endpoint.ip is not None:
        addresses = [endpoint.ip]
    else:
        try:
            addresses = await resolve_host(endpoint.host, endpoint.port)
        except OSError:
            raise UnsafeEndpointError("The API URL host name could not be resolved.") from None
        if not addresses:
            raise UnsafeEndpointError("The API URL host name could not be resolved.")
    for address in addresses:
        check_address(address, trusted=trusted, scheme=endpoint.scheme)
        if policy.private_only and not any(address in network for network in _TRUSTABLE_NETWORKS):
            raise UnsafeEndpointError("Local-only policy permits only trusted private model endpoints.")
    return list(dict.fromkeys(addresses))


async def validate_endpoint(raw: str, policy: EndpointPolicy) -> Endpoint:
    """Full check for a URL an administrator submits: syntax, policy and current DNS answers."""
    endpoint = normalize_base_url(raw)
    check_endpoint(endpoint, policy)
    await resolve_checked(endpoint, policy)
    return endpoint


# ------------------------------------------------------------------ HTTP client


def _inner_backend() -> httpcore2.AsyncNetworkBackend:
    """The real network backend. Tests replace this function with an in-memory one."""
    return httpcore2.AnyIOBackend()


class GuardedNetworkBackend(httpcore2.AsyncNetworkBackend):
    """Resolves, checks and connects to the checked address in one step (no rebinding window)."""

    def __init__(self, endpoint: Endpoint, policy: EndpointPolicy) -> None:
        self._endpoint = endpoint
        self._policy = policy
        self._inner = _inner_backend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        if (host.strip("[]").lower(), port) != (self._endpoint.host, self._endpoint.port):
            raise UnsafeEndpointError("Connections are allowed only to the configured API URL.")
        check_endpoint(self._endpoint, self._policy)  # the deployment policy may have changed since saving
        address = (await resolve_checked(self._endpoint, self._policy))[0]
        return await self._inner.connect_tcp(
            str(address), port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise UnsafeEndpointError("Unix sockets are not allowed for provider endpoints.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx2.AsyncHTTPTransport):
    """An httpx transport that only talks to one validated origin, through `GuardedNetworkBackend`."""

    def __init__(self, endpoint: Endpoint, policy: EndpointPolicy) -> None:
        super().__init__(trust_env=False)
        self._endpoint = endpoint
        # Replace the default pool: same TLS defaults (hostname verification stays on), guarded connects.
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx2.create_ssl_context(),
            network_backend=GuardedNetworkBackend(endpoint, policy),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        target = (url.scheme, url.raw_host.decode("ascii").strip("[]").lower(), url.port or _DEFAULT_PORTS.get(url.scheme, 0))
        if target != (self._endpoint.scheme, self._endpoint.host, self._endpoint.port):
            blocked = UnsafeEndpointError("Requests are allowed only to the configured API URL.")
            raise httpx2.ConnectError(blocked.message, request=request) from blocked
        return await super().handle_async_request(request)


def guarded_http_client(endpoint: Endpoint, policy: EndpointPolicy) -> httpx2.AsyncClient:
    """HTTP client for a custom provider: no redirects, no environment proxies or netrc, one origin only."""
    return httpx2.AsyncClient(
        transport=GuardedTransport(endpoint, policy), follow_redirects=False, trust_env=False
    )


def blocked_reason(exc: BaseException) -> UnsafeEndpointError | None:
    """The `UnsafeEndpointError` behind a wrapped client exception, if any."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, UnsafeEndpointError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None

