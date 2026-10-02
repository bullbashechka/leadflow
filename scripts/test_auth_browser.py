"""Run browser access checks against an isolated PostgreSQL database and local servers."""

import argparse
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, input=None, env=None):
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, input=input,
                            env=env, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Docker test setup failed; check Docker and the local environment.")
    return result.stdout.strip()


def python(code, *, env=None, input=None):
    return docker("run", "--rm", "-T", "--no-deps", "-e", "DATABASE_URL=",
                  "-e", "POSTGRES_HOST=postgres", "-e", "POSTGRES_PORT=5432",
                  "api", "python", "-c", code,
                  env=env, input=input)


def wait_url(url):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("A temporary test server did not become ready.")


def public_bot_url():
    if "VITE_TELEGRAM_BOT_URL" in os.environ:
        return os.environ["VITE_TELEGRAM_BOT_URL"].strip()
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        name, separator, configured = line.partition("=")
        if separator and name == "VITE_TELEGRAM_BOT_URL":
            return configured.strip()
    return ""


def run(artifacts):
    for port in (18003, 15173):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    identifier = uuid.uuid4().hex[:10]
    artifacts = artifacts / identifier
    artifacts.mkdir(parents=True, exist_ok=False)
    database = f"leadflow_auth_test_{identifier}"
    api_name = f"leadflow-auth-api-{identifier}"
    frontend_name = f"leadflow-auth-ui-{identifier}"
    created = False
    api_started = False
    frontend_started = False
    password = secrets.token_urlsafe(24)
    encoded = python("import sys; from django.contrib.auth.hashers import make_password; "
                     "print(make_password(sys.stdin.read()))", input=password)
    env = os.environ.copy()
    env["CRM_DEMO_PASSWORD_HASH"] = encoded
    env["CRM_TEST_PASSWORD"] = password
    env["CRM_TEST_BASE_URL"] = "http://localhost:15173"
    env["CRM_TEST_ARTIFACTS"] = str(artifacts)
    env["VITE_TELEGRAM_BOT_URL"] = public_bot_url()
    env["CRM_TEST_BOT_URL"] = env["VITE_TELEGRAM_BOT_URL"]
    database_sql = (
        "import django; django.setup(); from django.db import connection; "
        "from psycopg import sql; "
    )
    try:
        python(database_sql + f"connection.cursor().execute(sql.SQL('CREATE DATABASE {{}}').format(sql.Identifier('{database}')))")
        created = True
        startup = (
            "import os,subprocess; "
            "subprocess.run(['python','manage.py','migrate','--noinput'],check=True); "
            "os.execvp('python',['python','manage.py','runserver','0.0.0.0:8000','--noreload'])"
        )
        docker("run", "--rm", "-d", "--no-deps", "--name", api_name,
               "-p", "127.0.0.1:18003:8000", "-e", "CRM_DEMO_PASSWORD_HASH",
               "-e", f"POSTGRES_DB={database}",
               "-e", "DATABASE_URL=", "-e", "POSTGRES_HOST=postgres", "-e", "POSTGRES_PORT=5432",
               "-e", "DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:15173",
               "api", "python", "-c", startup, env=env)
        api_started = True
        wait_url("http://localhost:18003/api/health/")
        docker("run", "--rm", "-d", "--no-deps", "--name", frontend_name,
               "-p", "127.0.0.1:15173:5173", "-e", f"API_PROXY_TARGET=http://{api_name}:8000",
               "-e", "VITE_TELEGRAM_BOT_URL",
               "frontend", "npm", "run", "dev", "--", "--host", "0.0.0.0")
        frontend_started = True
        wait_url(env["CRM_TEST_BASE_URL"])
        result = subprocess.run(["npm", "run", "test:browser"], cwd=ROOT / "frontend", env=env,
                                text=True, capture_output=True, check=False)
        # Playwright failure logs can include entered values. Always redact test credentials.
        print((result.stdout + result.stderr).replace(password, "[redacted]").replace(encoded, "[redacted]"))
        print(f"Browser screenshots: {artifacts}")
        return result.returncode
    finally:
        for started, name in ((frontend_started, frontend_name), (api_started, api_name)):
            if started:
                subprocess.run(["docker", "stop", name], capture_output=True, check=False)
        if created:
            python(database_sql + f"connection.cursor().execute(sql.SQL('DROP DATABASE {{}} WITH (FORCE)').format(sql.Identifier('{database}')))")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, help="Optional temporary directory for screenshots")
    arguments = parser.parse_args()
    if arguments.artifacts:
        arguments.artifacts.mkdir(parents=True, exist_ok=True)
        result = run(arguments.artifacts.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="leadflow-auth-browser-") as directory:
            result = run(Path(directory))
    raise SystemExit(result)


if __name__ == "__main__":
    main()
