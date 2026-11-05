"""Public URL validation and DNS-pinned outbound HTTP for research fetches."""
from __future__ import annotations

import ipaddress
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidProxyURL, RequestException
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


def _connect_public_socket(host: str, port: int, timeout: float):
    """Open a socket to a validated numeric DNS answer, never to ``host``."""
    candidates = _resolve_public_addresses(host, port)
    last_error = None
    for numeric_address in candidates:
        try:
            return socket.create_connection((numeric_address, port), timeout=timeout)
        except OSError as exc:
            last_error = exc
    raise OSError("could not connect to a validated public address") from last_error


def _proxy_authority(authority: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit("//" + (authority or ""))
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid proxy destination")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid proxy destination")
    port = parsed.port or default_port
    if port not in {80, 443}:
        raise ValueError("non-standard destination port is blocked")
    return parsed.hostname.rstrip("."), port


class _DNSPinningProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DNSPinningHTTPProxy:
    """Loopback-only HTTP/CONNECT proxy for browser traffic.

    HTTPS is tunneled without TLS interception, so the browser retains normal
    certificate checks and SNI. Both HTTP requests and CONNECT tunnels resolve
    the destination on this server, reject any non-public DNS answer, and open
    the socket to a validated numeric address. The browser itself never gets a
    direct destination connection.
    """

    _MAX_REQUEST_BODY = 2 * 1024 * 1024
    _HOP_BY_HOP_HEADERS = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }

    def __init__(self, connect_timeout: float = 10, read_timeout: float = 30):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _error(self, status: int, message: str):
                payload = (message + "\n").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def do_CONNECT(self):
                upstream = None
                try:
                    host, port = _proxy_authority(self.path, 443)
                    scheme = "https" if port == 443 else "http"
                    host_for_url = f"[{host}]" if ":" in host else host
                    if not is_public_http_url(f"{scheme}://{host_for_url}:{port}/"):
                        raise ValueError("blocked non-public or invalid URL")
                    upstream = _connect_public_socket(host, port, owner.connect_timeout)
                except (OSError, ValueError):
                    self._error(403, "blocked unsafe or unresolvable destination")
                    return

                try:
                    self.connection.settimeout(owner.read_timeout)
                    upstream.settimeout(owner.read_timeout)
                    self.wfile.write(
                        b"HTTP/1.1 200 Connection Established\r\n"
                        b"Proxy-Agent: DNS-Pinned-Research\r\n\r\n"
                    )
                    self.wfile.flush()
                    sockets = (self.connection, upstream)
                    while True:
                        readable, _writable, exceptional = select.select(
                            sockets, (), sockets, owner.read_timeout
                        )
                        if exceptional or not readable:
                            break
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(data)
                except (OSError, ValueError):
                    pass
                finally:
                    upstream.close()
                    self.close_connection = True

            def _forward_http(self):
                response = None
                session = None
                headers_sent = False
                try:
                    parsed = urlsplit(self.path)
                    if parsed.scheme.lower() != "http" or not parsed.netloc:
                        raise ValueError("expected an absolute HTTP URL")
                    if not is_public_http_url(self.path):
                        raise ValueError("blocked non-public or invalid URL")
                    if self.command not in {"GET", "HEAD", "OPTIONS", "POST"}:
                        self._error(405, "HTTP method is blocked")
                        return
                    if self.headers.get("Transfer-Encoding"):
                        self._error(400, "chunked request bodies are not supported")
                        return
                    body_size = int(self.headers.get("Content-Length", "0") or "0")
                    if body_size < 0 or body_size > owner._MAX_REQUEST_BODY:
                        self._error(413, "request body is too large")
                        return
                    body = self.rfile.read(body_size) if body_size else None
                    headers = {
                        name: value
                        for name, value in self.headers.items()
                        if name.lower() not in owner._HOP_BY_HOP_HEADERS
                        and name.lower() not in {"host", "content-length"}
                    }
                    session = create_public_session()
                    response = session.request(
                        self.command,
                        self.path,
                        headers=headers,
                        data=body,
                        allow_redirects=False,
                        stream=True,
                        timeout=(owner.connect_timeout, owner.read_timeout),
                    )

                    raw_headers = response.raw.headers
                    length = response.headers.get("Content-Length")
                    no_body = self.command == "HEAD" or response.status_code in {204, 304}
                    chunked = not length and not no_body
                    self.send_response(response.status_code, response.reason)
                    items = raw_headers.iteritems() if hasattr(raw_headers, "iteritems") else response.headers.items()
                    for name, value in items:
                        lower_name = name.lower()
                        if lower_name in owner._HOP_BY_HOP_HEADERS or lower_name == "content-length":
                            continue
                        self.send_header(name, value)
                    if length:
                        self.send_header("Content-Length", length)
                    elif no_body:
                        self.send_header("Content-Length", "0")
                    elif chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True

                    if not no_body:
                        for data in response.raw.stream(65536, decode_content=False):
                            if data:
                                if chunked:
                                    self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                                    self.wfile.write(data + b"\r\n")
                                else:
                                    self.wfile.write(data)
                        if chunked:
                            self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except ValueError:
                    if not headers_sent:
                        try:
                            self._error(403, "blocked non-public or invalid URL")
                        except OSError:
                            pass
                except (OSError, RequestException):
                    if not headers_sent:
                        try:
                            self._error(502, "upstream request failed")
                        except OSError:
                            pass
                finally:
                    if response is not None:
                        response.close()
                    if session is not None:
                        session.close()
                    self.close_connection = True

            do_GET = _forward_http
            do_HEAD = _forward_http
            do_OPTIONS = _forward_http
            do_POST = _forward_http

            def do_PUT(self):
                self._error(405, "HTTP method is blocked")

            do_DELETE = do_PUT
            do_PATCH = do_PUT

        self._server = _DNSPinningProxyHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


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
