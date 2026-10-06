"""Shared settings adapted from Cookiecutter Django; see generation.json."""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
TIME_ZONE = "UTC"
LANGUAGE_CODE = "ru"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
if env("DATABASE_URL", default=""):
    DATABASES = {"default": env.db("DATABASE_URL")}
elif env("DJANGO_SETTINGS_MODULE", default="") == "config.settings.production":
    raise ImproperlyConfigured("Production requires DATABASE_URL; no local DB fallback.")
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER"),
            "PASSWORD": env("POSTGRES_PASSWORD"),
            "HOST": env("POSTGRES_HOST", default="postgres"),
            "PORT": env("POSTGRES_PORT", default="5432"),
            "CONN_MAX_AGE": 0,
        }
    }
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "leadflow.users",
    "leadflow.crm",
    "leadflow.bot",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "admin:login"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
X_FRAME_OPTIONS = "DENY"
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["leadflow.crm.api.access.CRMSessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["leadflow.crm.api.access.CRMAccessRequired"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "leadflow.crm.api.errors.exception_handler",
}
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
BOT_TOKEN = env("BOT_TOKEN", default="")
CRM_DEMO_PASSWORD_HASH = env("CRM_DEMO_PASSWORD_HASH", default="")
CRM_AUTH_MODE = env("CRM_AUTH_MODE", default="individual")
TRUSTED_PROXY_CIDRS = env.list("DJANGO_TRUSTED_PROXY_CIDRS", default=[])
INGRESS_SHARED_SECRET = env("INGRESS_SHARED_SECRET", default="")
REQUIRE_AUTHENTICATED_API_INGRESS = False
CLIENT_IP_REQUIRE_VERIFIED_INGRESS = False
ADMIN_REQUIRE_NETWORK_ALLOWLIST = False
ADMIN_NETWORK_ALLOWLIST = env.list("DJANGO_ADMIN_NETWORK_ALLOWLIST", default=[])
CSRF_FAILURE_VIEW = "leadflow.crm.api.errors.csrf_failure"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_SAVE_EVERY_REQUEST = False
MIDDLEWARE.insert(1, "leadflow.crm.api.errors.APINoStoreMiddleware")
MIDDLEWARE.insert(2, "leadflow.crm.api.errors.APIBodyLimitMiddleware")
DATA_UPLOAD_MAX_MEMORY_SIZE = 128 * 1024
DATABASES["default"].setdefault("OPTIONS", {}).update(
    {
        "connect_timeout": env.int("DB_CONNECT_TIMEOUT_SECONDS", default=3),
        "options": (
            f"-c statement_timeout={env.int('DB_STATEMENT_TIMEOUT_MS', default=8000)} "
            f"-c lock_timeout={env.int('DB_LOCK_TIMEOUT_MS', default=2000)}"
        ),
    }
)
MIDDLEWARE.append("leadflow.crm.api.access.AdminSecurityMiddleware")
