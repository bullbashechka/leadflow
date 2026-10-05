"""Authenticate Worker metadata before Django consumes forwarding headers."""

import ipaddress
from secrets import compare_digest

from django.conf import settings
from django.http import JsonResponse


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        metadata = request.META
        metadata.pop("LEADFLOW_CLIENT_IP", None)
        secret = metadata.pop("HTTP_X_LEADFLOW_INGRESS_SECRET", "")
        supplied_client = metadata.pop("HTTP_X_LEADFLOW_CLIENT_IP", "")
        trusted_peer = False
        try:
            peer = ipaddress.ip_address(metadata.get("REMOTE_ADDR", ""))
            trusted_peer = any(
                peer in ipaddress.ip_network(cidr, strict=False)
                for cidr in settings.TRUSTED_PROXY_CIDRS
            )
        except ValueError:
            pass

        # The platform must overwrite this header. An arbitrary direct peer cannot
        # opt out of HTTPS redirects by supplying its own forwarded protocol.
        if not trusted_peer or metadata.get("HTTP_X_FORWARDED_PROTO") not in {"http", "https"}:
            metadata.pop("HTTP_X_FORWARDED_PROTO", None)
        metadata.pop("HTTP_X_FORWARDED_HOST", None)
        metadata.pop("HTTP_FORWARDED", None)

        configured_secret = settings.INGRESS_SHARED_SECRET
        if (
            trusted_peer
            and configured_secret
            and secret.isascii()
            and compare_digest(secret, configured_secret)
        ):
            try:
                metadata["LEADFLOW_CLIENT_IP"] = str(ipaddress.ip_address(supplied_client))
            except ValueError:
                pass

        api = request.path == "/api" or request.path.startswith("/api/")
        if (
            settings.REQUIRE_AUTHENTICATED_API_INGRESS
            and api
            and request.path != "/api/health/"
            and "LEADFLOW_CLIENT_IP" not in metadata
        ):
            response = JsonResponse(
                {
                    "code": "permission_denied",
                    "message": "Verified ingress is required.",
                    "field_errors": {},
                },
                status=403,
            )
            response["Cache-Control"] = "no-store"
            return response
        return self.get_response(request)
