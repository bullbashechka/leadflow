"""Tests use a separate PostgreSQL database through pytest-django."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-key")
from .base import *  # noqa: E402, F403

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
