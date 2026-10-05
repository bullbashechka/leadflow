"""Reject production configuration that cannot meet the deployment contract."""

import ipaddress
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def validate_networks(values, name, *, required=False):
    if required and not values:
        raise ImproperlyConfigured(f"{name} must list the verified ingress proxy CIDRs.")
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as error:
            raise ImproperlyConfigured(f"{name} contains an invalid CIDR.") from error
        if network.prefixlen == 0:
            raise ImproperlyConfigured(f"{name} must not trust an unrestricted network.")


def validate_production_settings(env, database):
    """Validate without connecting to the database or returning secret values."""
    secret = env("DJANGO_SECRET_KEY", default="")
    if len(secret) < 50 or len(set(secret)) < 5 or secret.startswith("django-insecure-"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a strong production secret.")

    hosts = env.list("DJANGO_ALLOWED_HOSTS", default=[])
    hostname = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?")
    if not hosts or any(not hostname.fullmatch(host) for host in hosts):
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must contain exact hostnames.")

    origins = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
    if not origins:
        raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must contain exact origins.")
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme != "https":
            raise ImproperlyConfigured("CSRF trusted origins must use HTTPS.")
        if not parsed.hostname or "*" in origin or parsed.username or parsed.password:
            raise ImproperlyConfigured("CSRF trusted origins must be exact HTTPS origins.")
        if parsed.path or parsed.query or parsed.fragment:
            raise ImproperlyConfigured("CSRF trusted origins must contain only the origin.")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ImproperlyConfigured("CSRF trusted origin has an invalid port.") from error

    validate_networks(
        env.list("DJANGO_TRUSTED_PROXY_CIDRS", default=[]),
        "DJANGO_TRUSTED_PROXY_CIDRS",
        required=True,
    )
    validate_networks(
        env.list("DJANGO_ADMIN_NETWORK_ALLOWLIST", default=[]),
        "DJANGO_ADMIN_NETWORK_ALLOWLIST",
    )
    ingress_secret = env("INGRESS_SHARED_SECRET", default="")
    if len(ingress_secret) < 32 or not re.fullmatch(r"[\x21-\x7e]+", ingress_secret):
        raise ImproperlyConfigured("INGRESS_SHARED_SECRET must contain at least 32 characters.")
    if env("CRM_AUTH_MODE", default="individual") != "individual":
        raise ImproperlyConfigured("Production CRM_AUTH_MODE must be individual.")

    if not env("DATABASE_URL", default=""):
        raise ImproperlyConfigured("Production requires DATABASE_URL; no local DB fallback.")
    if database.get("ENGINE") != "django.db.backends.postgresql":
        raise ImproperlyConfigured("Production DATABASE_URL must use PostgreSQL.")
    options = database.get("OPTIONS", {})
    if options.get("sslmode") != "verify-full":
        raise ImproperlyConfigured("Production DATABASE_URL requires sslmode=verify-full.")
    certificate = options.get("sslrootcert", "")
    if not certificate or not Path(certificate).is_file():
        raise ImproperlyConfigured("Production DATABASE_URL requires a readable sslrootcert file.")
    try:
        ssl.create_default_context(cafile=certificate)
    except (OSError, ssl.SSLError) as error:
        raise ImproperlyConfigured(
            "Production sslrootcert must contain trusted CA certificates."
        ) from error
