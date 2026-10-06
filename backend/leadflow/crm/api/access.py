import base64
import math
import re
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.db import transaction
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.crypto import salted_hmac
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission

from leadflow.crm.api.errors import APIError
from leadflow.crm.api.source import address
from leadflow.crm.api.source import client_source
from leadflow.crm.api.source import in_networks
from leadflow.crm.api.source import networks
from leadflow.crm.models import LoginAttempt

DEMO_USERNAME = "__leadflow_demo__"
ACCESS_KEY = "crm_access"
EXPIRY_KEY = "crm_expires_at"
VERSION_KEY = "crm_password_version"
MODE_KEY = "crm_auth_mode"
REVISION_KEY = "crm_auth_revision"
SESSION_DURATION = timedelta(hours=48)


@lru_cache(maxsize=8)
def validated_hash(encoded):
    """Treat malformed environment values as unavailable, never as credentials."""
    try:
        hasher = identify_hasher(encoded)
        decoded = hasher.decode(encoded)
        digest = decoded["hash"]
        if not decoded.get("salt") or not digest:
            return None
        if hasher.algorithm == "md5":
            if not re.fullmatch("[0-9a-f]{32}", digest):
                return None
        else:
            raw = base64.b64decode(digest + "=" * (-len(digest) % 4), validate=True)
            if len(raw) < 16 or decoded.get("iterations", 1) < 1:
                return None
        return encoded
    except Exception:
        # Hasher implementations have different exceptions for malformed encodings.
        return None


def password_version(encoded):
    return salted_hmac("leadflow.crm.password", encoded, algorithm="sha256").hexdigest()


def crm_auth_mode():
    mode = settings.CRM_AUTH_MODE
    if mode not in {"individual", "demo"}:
        raise APIError("configuration_error", "Вход временно недоступен.", status=503)
    return mode


def access_expiry(request):
    session = request.session
    if not session.get(ACCESS_KEY):
        return None
    mode = crm_auth_mode()
    user = getattr(request, "_request", request).user
    encoded = (
        validated_hash(settings.CRM_DEMO_PASSWORD_HASH)
        if mode == "demo"
        else user.password
        if user.is_authenticated and user.has_usable_password()
        else None
    )
    try:
        expires = timezone.datetime.fromisoformat(session[EXPIRY_KEY])
        valid = (
            encoded
            and timezone.is_aware(expires)
            and expires > timezone.now()
            and user.is_authenticated
            and user.is_active
            and not user.is_staff
            and not user.is_superuser
            and session.get(MODE_KEY) == mode
            and session.get(REVISION_KEY) == user.auth_revision
            and (
                (user.username == DEMO_USERNAME and not user.has_usable_password())
                if mode == "demo"
                else user.username != DEMO_USERNAME and user.has_usable_password()
            )
            and constant_time_compare(session.get(VERSION_KEY, ""), password_version(encoded))
        )
    except KeyError, TypeError, ValueError:
        valid = False
    if not valid:
        session.flush()
        return None
    return expires


def enforce_csrf(request):
    underlying = getattr(request, "_request", request)
    check = CsrfViewMiddleware(lambda _: None)
    check.process_request(underlying)
    rejection = check.process_view(underlying, lambda _: None, (), {})
    if rejection is not None:
        raise APIError("csrf_failed", "Обновите доступ и повторите действие.", status=403)


class SessionIdentity(SessionAuthentication):
    """Public discovery preserves Django's user; unsafe views enforce CSRF explicitly."""

    def authenticate(self, request):
        user = request._request.user
        return (user, None) if user.is_authenticated else None


class CRMSessionAuthentication(SessionAuthentication):
    def authenticate(self, request):
        if access_expiry(request) is None:
            return None
        enforce_csrf(request)
        return request._request.user, None

    def authenticate_header(self, request):
        return 'Session realm="crm"'


class CRMAccessRequired(BasePermission):
    def has_permission(self, request, view):
        return access_expiry(request) is not None


def record_login_attempt(request, *, scope="crm", username=None):
    source = client_source(request)
    buckets = [("source", source, 10)]
    if scope == "crm":
        buckets.append(("global", "crm", 60))
        if username is not None:
            buckets.append(("account", username.strip().casefold(), 20))
    keys = sorted(
        (
            salted_hmac(f"leadflow.{scope}.login-{kind}", value, algorithm="sha256").hexdigest(),
            limit,
        )
        for kind, value, limit in buckets
    )
    now = timezone.now()
    with transaction.atomic():
        counters = []
        for key, limit in keys:
            LoginAttempt.objects.get_or_create(source_key=key, defaults={"started_at": now})
            counter = LoginAttempt.objects.select_for_update().get(pk=key)
            if now >= counter.started_at + timedelta(minutes=1):
                counter.started_at = now
                counter.attempts = 0
            if counter.attempts >= limit:
                wait = max(
                    1, math.ceil((counter.started_at + timedelta(minutes=1) - now).total_seconds())
                )
                raise APIError(
                    "rate_limited",
                    "Слишком много попыток. Подождите и повторите вход.",
                    status=429,
                    retry_after=wait,
                )
            counters.append(counter)
        for counter in counters:
            counter.attempts += 1
            counter.save(update_fields=["started_at", "attempts"])


class AdminSecurityMiddleware:
    """Keep technical administration behind a network policy and its own login limit."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/admin/") and settings.ADMIN_REQUIRE_NETWORK_ALLOWLIST:
            allowed = networks(settings.ADMIN_NETWORK_ALLOWLIST)
            peer = address(request.META.get("REMOTE_ADDR"))
            trusted = networks(settings.TRUSTED_PROXY_CIDRS)
            # A shared proxy's address cannot establish an administrator's network identity.
            unverified_proxy = (
                settings.CLIENT_IP_REQUIRE_VERIFIED_INGRESS
                and in_networks(peer, trusted)
                and address(request.META.get("LEADFLOW_CLIENT_IP")) is None
            )
            if unverified_proxy or not in_networks(address(client_source(request)), allowed):
                return HttpResponse("Доступ к администрированию запрещён.", status=403)
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.path == "/admin/login/" and request.method == "POST":
            try:
                record_login_attempt(request, scope="admin")
            except APIError as error:
                response = HttpResponse("Слишком много попыток входа. Повторите позже.", status=429)
                response["Retry-After"] = str(error.retry_after)
                return response
        return None
