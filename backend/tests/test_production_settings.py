"""Production startup must reject insecure configuration without opening the DB."""

import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import RequestFactory
from django.test import override_settings


@pytest.fixture
def production_environment():
    certificate = ssl.get_default_verify_paths().cafile
    assert certificate and Path(certificate).is_file()
    return {
        "PATH": os.environ["PATH"],
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "DJANGO_SECRET_KEY": "production-config-test-only-key-" * 3,
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "https://crm.example.com",
        "DJANGO_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
        "INGRESS_SHARED_SECRET": "test-ingress-secret-not-for-deployment-" * 2,
        "CRM_AUTH_MODE": "individual",
        "DATABASE_URL": (
            "postgres://test_user:test_password@db.example.com/test"
            f"?sslmode=verify-full&sslrootcert={certificate}"
        ),
    }


def import_production(environment):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json; import config.settings.production as p; "
            "print(json.dumps({'debug': p.DEBUG, 'secure': p.SESSION_COOKIE_SECURE, "
            "'ingress': p.REQUIRE_AUTHENTICATED_API_INGRESS, "
            "'auth': p.CRM_AUTH_MODE, 'admin_closed': p.ADMIN_REQUIRE_NETWORK_ALLOWLIST, "
            "'hsts': getattr(p, 'SECURE_HSTS_SECONDS', 0)}))",
        ],
        env=environment,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_valid_production_configuration_is_secure(production_environment):
    result = import_production(production_environment)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "debug": False,
        "secure": True,
        "ingress": True,
        "auth": "individual",
        "admin_closed": True,
        "hsts": 31536000,
    }


def test_invalid_database_url_does_not_log_credentials(production_environment):
    password = "credential-marker-must-not-be-logged"
    production_environment["DATABASE_URL"] = f"unknown://test_user:{password}@db.example.com/db"
    result = import_production(production_environment)
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert password not in result.stderr


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DJANGO_SECRET_KEY", "", "DJANGO_SECRET_KEY"),
        ("DJANGO_SECRET_KEY", "short", "DJANGO_SECRET_KEY"),
        ("DJANGO_ALLOWED_HOSTS", "", "DJANGO_ALLOWED_HOSTS"),
        ("DJANGO_ALLOWED_HOSTS", "*", "DJANGO_ALLOWED_HOSTS"),
        ("DJANGO_ALLOWED_HOSTS", ".example.com", "DJANGO_ALLOWED_HOSTS"),
        ("DJANGO_CSRF_TRUSTED_ORIGINS", "", "DJANGO_CSRF_TRUSTED_ORIGINS"),
        ("DJANGO_CSRF_TRUSTED_ORIGINS", "http://crm.example.com", "HTTPS"),
        ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://*.example.com", "exact"),
        ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://crm.example.com/path", "origin"),
        ("DJANGO_TRUSTED_PROXY_CIDRS", "", "DJANGO_TRUSTED_PROXY_CIDRS"),
        ("DJANGO_TRUSTED_PROXY_CIDRS", "0.0.0.0/0", "unrestricted"),
        ("DJANGO_TRUSTED_PROXY_CIDRS", "::/0", "unrestricted"),
        ("DJANGO_TRUSTED_PROXY_CIDRS", "not-a-network", "CIDR"),
        ("INGRESS_SHARED_SECRET", "", "INGRESS_SHARED_SECRET"),
        ("INGRESS_SHARED_SECRET", "short", "INGRESS_SHARED_SECRET"),
        ("INGRESS_SHARED_SECRET", "секрет" * 10, "INGRESS_SHARED_SECRET"),
        ("CRM_AUTH_MODE", "demo", "individual"),
        ("DATABASE_URL", "", "DATABASE_URL"),
        ("DATABASE_URL", "postgres://u:p@db.example.com/db", "verify-full"),
        ("DATABASE_URL", "postgres://u:p@db.example.com/db?sslmode=disable", "verify-full"),
        ("DATABASE_URL", "postgres://u:p@db.example.com/db?sslmode=require", "verify-full"),
        ("DATABASE_URL", "postgres://u:p@db.example.com/db?sslmode=verify-full", "sslrootcert"),
        (
            "DATABASE_URL",
            "postgres://u:p@db.example.com/db?sslmode=verify-full&sslrootcert=/missing.pem",
            "sslrootcert",
        ),
    ],
)
def test_insecure_production_configuration_is_rejected(
    production_environment, name, value, message
):
    production_environment[name] = value
    result = import_production(production_environment)
    assert result.returncode != 0
    assert message in result.stderr


@override_settings(
    TRUSTED_PROXY_CIDRS=["192.0.2.0/24"],
    INGRESS_SHARED_SECRET="test-ingress-secret-not-for-deployment-" * 2,
    REQUIRE_AUTHENTICATED_API_INGRESS=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_SSL_REDIRECT=True,
    ALLOWED_HOSTS=["api.example.com"],
)
@pytest.mark.parametrize("peer", ["192.0.2.10", "198.51.100.10"])
def test_only_trusted_authenticated_ingress_can_reach_crm(peer):
    from config.trusted_proxy import TrustedProxyMiddleware

    request = RequestFactory().get(
        "/api/leads/",
        HTTP_HOST="api.example.com",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_LEADFLOW_INGRESS_SECRET="test-ingress-secret-not-for-deployment-" * 2,
        HTTP_X_LEADFLOW_CLIENT_IP="203.0.113.9",
        REMOTE_ADDR=peer,
    )
    middleware = TrustedProxyMiddleware(SecurityMiddleware(lambda r: HttpResponse("ok")))
    response = middleware(request)
    if peer == "192.0.2.10":
        assert response.status_code == 200
        assert request.is_secure()
        assert request.META["LEADFLOW_CLIENT_IP"] == "203.0.113.9"
    else:
        assert response.status_code == 403
        assert "LEADFLOW_CLIENT_IP" not in request.META
    assert "HTTP_X_LEADFLOW_INGRESS_SECRET" not in request.META
    assert "HTTP_X_LEADFLOW_CLIENT_IP" not in request.META


@override_settings(
    TRUSTED_PROXY_CIDRS=["192.0.2.0/24"],
    INGRESS_SHARED_SECRET="test-ingress-secret-not-for-deployment-" * 2,
    REQUIRE_AUTHENTICATED_API_INGRESS=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_SSL_REDIRECT=True,
    ALLOWED_HOSTS=["api.example.com"],
)
def test_direct_api_is_closed_and_health_remains_available():
    from config.trusted_proxy import TrustedProxyMiddleware

    middleware = TrustedProxyMiddleware(SecurityMiddleware(lambda r: HttpResponse("ok")))
    factory = RequestFactory()
    api = factory.get(
        "/api/auth/session/",
        HTTP_HOST="api.example.com",
        REMOTE_ADDR="192.0.2.10",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_LEADFLOW_CLIENT_IP="203.0.113.9",
    )
    assert middleware(api).status_code == 403
    health = factory.get(
        "/api/health/",
        HTTP_HOST="api.example.com",
        REMOTE_ADDR="192.0.2.10",
        HTTP_X_FORWARDED_PROTO="https",
    )
    assert middleware(health).status_code == 200
    untrusted = factory.get(
        "/api/health/",
        HTTP_HOST="api.example.com",
        REMOTE_ADDR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
    )
    assert middleware(untrusted).status_code == 301


@override_settings(
    TRUSTED_PROXY_CIDRS=["192.0.2.0/24"],
    INGRESS_SHARED_SECRET="test-ingress-secret-not-for-deployment-" * 2,
    REQUIRE_AUTHENTICATED_API_INGRESS=True,
)
@pytest.mark.parametrize("secret", ["wrong", "секрет", "", "with space"])
def test_invalid_ingress_secret_is_denied_without_server_error(secret):
    from config.trusted_proxy import TrustedProxyMiddleware

    request = RequestFactory().get(
        "/api/leads/",
        REMOTE_ADDR="192.0.2.10",
        HTTP_X_LEADFLOW_INGRESS_SECRET=secret,
        HTTP_X_LEADFLOW_CLIENT_IP="203.0.113.9",
    )
    response = TrustedProxyMiddleware(lambda r: HttpResponse("ok"))(request)
    assert response.status_code == 403
    assert "LEADFLOW_CLIENT_IP" not in request.META
    assert response["Cache-Control"] == "no-store"
