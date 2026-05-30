"""Security utilities: URL validation, rate limiting."""
import ipaddress
import time
import logging
from collections import defaultdict

_ERR_URL_EMPTY = "URL must not be empty."
_METADATA_HOST = "metadata.google.internal"
from threading import Lock
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL / SSRF validation
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),     # loopback
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("10.0.0.0/8"),       # private
    ipaddress.ip_network("172.16.0.0/12"),    # private
    ipaddress.ip_network("192.168.0.0/16"),   # private
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]


def _is_blocked_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        return False  # hostname — DNS resolution not checked here


def validate_docker_host_url(url: str) -> str | None:
    """Return an error string if the Docker host URL contains obviously dangerous targets, else None.

    unix:// and private TCP addresses are legitimate Docker use cases.
    We only block the most dangerous targets: cloud metadata endpoints and localhost HTTP.
    """
    if not url:
        return _ERR_URL_EMPTY
    url_lower = url.lower().strip()
    # Block AWS/GCP metadata endpoint explicitly
    if "169.254.169.254" in url_lower:
        return "URL targets a reserved cloud metadata address."
    if _METADATA_HOST in url_lower:
        return "URL targets a reserved cloud metadata address."
    # Disallow http/https schemes — Docker uses unix://, tcp://, or ssh://
    if url_lower.startswith("http://") or url_lower.startswith("https://"):
        return "Docker host URL must use tcp://, unix://, or ssh:// — not http/https."
    return None


def validate_webhook_url(url: str) -> str | None:
    """Return an error string if the URL is not safe for outbound webhook calls, else None."""
    if not url:
        return _ERR_URL_EMPTY
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format."

    if parsed.scheme not in ("http", "https"):
        return "URL must use http or https."

    host = parsed.hostname
    if not host:
        return "URL has no host."

    if _is_blocked_ip(host):
        return "URL resolves to a private or reserved IP address."

    # Block obvious internal hostnames
    if host in ("localhost", "metadata", _METADATA_HOST):
        return "URL points to an internal host."

    return None


def validate_oidc_url(url: str) -> str | None:
    """Return an error string if the OIDC provider URL is unsafe, else None."""
    if not url:
        return _ERR_URL_EMPTY
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format."

    if parsed.scheme not in ("http", "https"):
        return "OIDC provider URL must use http or https."

    host = parsed.hostname
    if not host:
        return "URL has no host."

    if _is_blocked_ip(host):
        return "OIDC provider URL resolves to a private or reserved IP address."

    if host in ("localhost", "metadata", _METADATA_HOST):
        return "OIDC provider URL points to an internal host."

    return None


# ---------------------------------------------------------------------------
# Simple in-memory login rate limiter
# ---------------------------------------------------------------------------

_RATE_WINDOW_SECONDS = 60
_RATE_MAX_ATTEMPTS = 10

_rate_data: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def check_login_rate_limit(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login, False if rate-limited."""
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS
    with _rate_lock:
        attempts = _rate_data[ip]
        # Remove old attempts outside the window
        _rate_data[ip] = [t for t in attempts if t > cutoff]
        if len(_rate_data[ip]) >= _RATE_MAX_ATTEMPTS:
            return False
        _rate_data[ip].append(now)
        return True


def record_failed_login(ip: str) -> None:
    """Record a failed attempt WITHOUT consuming the rate-limit slot (already done in check)."""
    pass  # slot already recorded in check_login_rate_limit
