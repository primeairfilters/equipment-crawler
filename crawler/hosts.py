"""Host names, scope, and the hosts we never touch."""
import re
from urllib.parse import urlsplit

# AHRI's sites are never fetched, even if someone types them into `ok`.
REFUSED_HOSTS = ("ahridirectory.org", "ahrinet.org")

_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$")


class RefusedHost(Exception):
    pass


def normalize_host(raw):
    """Lowercase, no trailing dot, punycode. Raises ValueError for anything but a bare host name."""
    if not isinstance(raw, str):
        raise ValueError("not a host name")
    host = raw.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValueError("empty host name")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"not a host name: {raw!r}") from exc
    if len(host) > 253 or not all(_LABEL.match(label) for label in host.split(".")):
        raise ValueError(f"not a host name: {raw!r}")
    return host


def _under(host, parent):
    return host == parent or host.endswith("." + parent)


def is_refused_host(host):
    try:
        host = normalize_host(host)
    except ValueError:
        return False
    return any(_under(host, refused) for refused in REFUSED_HOSTS)


def in_scope(url, approved_hosts):
    """True only for http(s) URLs whose host is an approved host or a dot-bounded subdomain of one."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return False
        host = normalize_host(parts.hostname or "")
    except ValueError:
        return False
    if is_refused_host(host):
        return False
    for approved in approved_hosts:
        try:
            if _under(host, normalize_host(approved)):
                return True
        except ValueError:
            continue
    return False
