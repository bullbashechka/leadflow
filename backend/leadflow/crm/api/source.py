"""Resolve client addresses only through an explicitly trusted ingress boundary."""

from ipaddress import ip_address
from ipaddress import ip_network

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def address(value):
    try:
        parsed = ip_address(value)
        return parsed.ipv4_mapped if getattr(parsed, "ipv4_mapped", None) else parsed
    except ValueError, TypeError:
        return None


def networks(values):
    try:
        return [ip_network(value, strict=False) for value in values]
    except ValueError, TypeError:
        raise ImproperlyConfigured("IP allowlists must contain valid CIDR networks") from None


def in_networks(candidate, allowed):
    return candidate is not None and any(candidate in network for network in allowed)


def client_source(request):
    # This key is server metadata written by TrustedProxyMiddleware, not an HTTP header.
    verified = address(request.META.get("LEADFLOW_CLIENT_IP"))
    if verified is not None:
        return str(verified)
    peer = address(request.META.get("REMOTE_ADDR"))
    if peer is None:
        return "unknown"
    if getattr(settings, "CLIENT_IP_REQUIRE_VERIFIED_INGRESS", False):
        return str(peer)
    trusted = networks(getattr(settings, "TRUSTED_PROXY_CIDRS", []))
    if not in_networks(peer, trusted):
        return str(peer)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not isinstance(forwarded, str):
        return str(peer)
    parts = forwarded.split(",")
    if len(parts) > 16:
        return str(peer)
    chain = [address(part.strip()) for part in parts]
    if not chain or any(item is None for item in chain):
        return str(peer)
    # Ignore spoofed left-hand prefixes beyond the closest untrusted hop.
    for candidate in reversed(chain):
        if not in_networks(candidate, trusted):
            return str(candidate)
    return str(chain[0])
