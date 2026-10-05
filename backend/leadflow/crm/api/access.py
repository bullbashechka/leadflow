import base64
import math
import re
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.db import transaction
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.crypto import salted_hmac
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission

from leadflow.crm.api.errors import APIError
from leadflow.crm.models import LoginAttempt

DEMO_USERNAME = "__leadflow_demo__"
ACCESS_KEY = "crm_access"
EXPIRY_KEY = "crm_expires_at"
VERSION_KEY = "crm_password_version"
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


def access_expiry(request):
    session = request.session
    if not session.get(ACCESS_KEY):
        return None
    encoded = validated_hash(settings.CRM_DEMO_PASSWORD_HASH)
    user = getattr(request, "_request", request).user
    try:
        expires = timezone.datetime.fromisoformat(session[EXPIRY_KEY])
        valid = (
            encoded
            and timezone.is_aware(expires)
            and expires > timezone.now()
            and user.is_authenticated
            and user.is_active
            and user.username == DEMO_USERNAME
            and not user.is_staff
            and not user.is_superuser
            and not user.has_usable_password()
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


def record_login_attempt(request):
    # Until deployment configures a trusted proxy chain, forwarding headers are untrusted.
    source = request.META.get("REMOTE_ADDR", "unknown")
    key = salted_hmac("leadflow.crm.login-source", source, algorithm="sha256").hexdigest()
    now = timezone.now()
    with transaction.atomic():
        LoginAttempt.objects.get_or_create(source_key=key, defaults={"started_at": now})
        counter = LoginAttempt.objects.select_for_update().get(pk=key)
        if now >= counter.started_at + timedelta(minutes=1):
            counter.started_at = now
            counter.attempts = 0
        if counter.attempts >= 10:
            wait = max(
                1, math.ceil((counter.started_at + timedelta(minutes=1) - now).total_seconds())
            )
            raise APIError(
                "rate_limited",
                "Слишком много попыток. Подождите и повторите вход.",
                status=429,
                retry_after=wait,
            )
        counter.attempts += 1
        counter.save(update_fields=["started_at", "attempts"])
