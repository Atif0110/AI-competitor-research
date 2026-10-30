"""Syntactic URL and redirect guards for outbound research requests."""
from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlsplit


def is_public_http_url(value: str) -> bool:
    """Return whether a URL is a syntactically safe public web destination.

    This blocks obvious SSRF targets at API and crawler boundaries. It does
    not perform DNS resolution; deployments should also restrict outbound
    network access to private/link-local ranges at the network layer.
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


def safe_http_get(url: str, *, max_redirects: int = 5, **kwargs):
    """GET a public URL, validating every redirect before following it."""
    import requests

    current = url
    for redirect_count in range(max_redirects + 1):
        if not is_public_http_url(current):
            raise ValueError("blocked non-public or invalid URL")
        response = requests.get(current, allow_redirects=False, **kwargs)
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
