"""Tiny HTTP helper (stdlib only, so the plugin has no pip dependencies)."""
import urllib.parse
import urllib.request

# Loopback traffic must never go through a system/corporate proxy.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_loopback(url):
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("localhost", "::1") or host.startswith("127.")


def urlopen(req, timeout):
    if _is_loopback(req.full_url):
        return _direct.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
