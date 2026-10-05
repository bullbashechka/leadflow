"""Fail-closed production settings for the authenticated Worker ingress."""

from config.production_checks import validate_production_settings

from .base import *  # noqa: F403
from .base import DATABASES
from .base import MIDDLEWARE
from .base import env

validate_production_settings(env, DATABASES["default"])

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
# This route returns only readiness status and also serves local platform probes.
SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
REQUIRE_AUTHENTICATED_API_INGRESS = True
CLIENT_IP_REQUIRE_VERIFIED_INGRESS = True
ADMIN_REQUIRE_NETWORK_ALLOWLIST = True

MIDDLEWARE = [
    "config.trusted_proxy.TrustedProxyMiddleware",
    MIDDLEWARE[0],
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
