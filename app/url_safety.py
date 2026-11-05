"""Public URL validation and DNS-pinned outbound HTTP for research fetches."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidProxyURL
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.poolmanager import PoolManager, pool_classes_by_scheme
from urllib3.util import connection as urllib3_connection


def is_public_http_url(value: str) -> bool:
    """Return whether a URL is syntactically an HTTP(S) public-web URL.

    DNS is deliberately checked at socket-connection time by
    :func:`create_public_session`; resolving here would leave a rebinding race
    between validation and the request.
    """
    try:
        parsed = urlsplit((value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in {None, 80, 443}:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "metadata.google.internal"} or host.endswith(
            (".localhost", ".local", ".internal", ".test", ".invalid", ".onion", ".home.arpa", ".arpa")
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "." in host and not host.startswith(".")
        return address.is_global
    except (ValueError, UnicodeError):
        return False


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host and reject the whole answer if any IP is non-public."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("hostname could not be resolved") from exc

    candidates: list[str] = []
    seen = set()
    for family, socktype, protocol, _canonical_name, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise ValueError("DNS returned an invalid address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_reserved:
            raise ValueError("hostname resolved to a non-public address")
        numeric_address = str(address)
        if numeric_address not in seen:
            seen.add(numeric_address)
            candidates.append(numeric_address)

    if not candidates:
        raise ValueError("hostname resolved to no usable public addresses")
    return candidates


class _DNSPinnedConnectionMixin:
    """Resolve once per new socket, validate every answer, then connect by IP.

    The HTTP Host header and HTTPS SNI still use urllib3's original hostname;
    only the TCP destination is pinned to the validated numeric address.
    """

    def _new_conn(self):
        try:
            candidates = _resolve_public_addresses(self._dns_host, self.port)
        except (OSError, ValueError) as exc:
            raise NewConnectionError(
                self,
                f"blocked unsafe or unresolvable destination for {self._dns_host}",
            ) from exc

        last_error = None
        for numeric_address in candidates:
            try:
                return urllib3_connection.create_connection(
                    (numeric_address, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
            except OSError as exc:
                last_error = exc

        raise NewConnectionError(
            self,
            f"could not connect to a validated public address for {self._dns_host}",
        ) from last_error


class _DNSPinnedHTTPConnection(_DNSPinnedConnectionMixin, HTTPConnection):
    pass


class _DNSPinnedHTTPSConnection(_DNSPinnedConnectionMixin, HTTPSConnection):
    pass


class _DNSPinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _DNSPinnedHTTPConnection


class _DNSPinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _DNSPinnedHTTPSConnection


class _DNSPinnedPoolManager(PoolManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # urllib3 copies its module-level map onto the instance during init.
        self.pool_classes_by_scheme = {
            **pool_classes_by_scheme,
            "http": _DNSPinnedHTTPConnectionPool,
            "https": _DNSPinnedHTTPSConnectionPool,
        }


class DNSPinnedHTTPAdapter(HTTPAdapter):
    """Requests adapter that pins DNS and refuses external proxy routing."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _DNSPinnedPoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy: str, **proxy_kwargs):
        raise InvalidProxyURL("proxies are disabled for public research URL fetches")


def create_public_session():
    """Create a requests session that cannot bypass the DNS-pinned adapter."""
    import requests

    session = requests.Session()
    session.trust_env = False
    adapter = DNSPinnedHTTPAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def public_fetch_guard_enabled() -> bool:
    """Whether unauthenticated callers can submit URLs to the research API."""
    from app.config import settings

    return settings.anonymous_access_enabled


def safe_http_get(url: str, *, max_redirects: int = 5, **kwargs):
    """GET a URL, pinning public-mode DNS and validating every redirect."""
    import requests

    current = url
    public_mode = public_fetch_guard_enabled()
    session = create_public_session() if public_mode else None
    if public_mode:
        # Configured and environment proxies resolve destinations outside this
        # process, so they cannot use the DNS-pinned socket guard.
        kwargs.pop("proxies", None)

    try:
        for redirect_count in range(max_redirects + 1):
            if not is_public_http_url(current):
                raise ValueError("blocked non-public or invalid URL")
            response = (
                session.get(current, allow_redirects=False, **kwargs)
                if session is not None
                else requests.get(current, allow_redirects=False, **kwargs)
            )
            if not (response.is_redirect or response.is_permanent_redirect):
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            response.close()
            if redirect_count >= max_redirects:
                break
            current = urljoin(current, location)
        raise ValueError("too many redirects")
    finally:
        if session is not None:
            session.close()
