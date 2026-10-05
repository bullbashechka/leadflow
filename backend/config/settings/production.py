"""Secure defaults; public deployment configuration is a later stage."""

from .base import *  # noqa: F403
from .base import MIDDLEWARE

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True

MIDDLEWARE = [MIDDLEWARE[0], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[1:]]
