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


def run(artifacts, grep=None, individual=False, project=None):
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
    env["CRM_AUTH_MODE"] = "individual" if individual else "demo"
    env["CRM_TEST_USERNAME"] = "crm-test-operator" if individual else ""
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
        startup_parts = [
            "import os,subprocess",
            "subprocess.run(['python','manage.py','migrate','--noinput'],check=True)",
        ]
        if individual:
            startup_parts.extend([
                "import django; django.setup()",
                "from django.contrib.auth import get_user_model",
                "get_user_model().objects.create_user("
                "username=os.environ['CRM_TEST_USERNAME'], "
                "password=os.environ['CRM_TEST_PASSWORD'])",
            ])
        startup_parts.append(
            "os.execvp('python',['python','manage.py','runserver','0.0.0.0:8000','--noreload'])"
        )
        startup = "; ".join(startup_parts)
        docker("run", "--rm", "-d", "--no-deps", "--name", api_name,
               "-p", "127.0.0.1:18003:8000", "-e", "CRM_DEMO_PASSWORD_HASH",
               "-e", "CRM_AUTH_MODE", "-e", "CRM_TEST_USERNAME", "-e", "CRM_TEST_PASSWORD",
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
        command = ["npm", "run", "test:browser"]
        selection = []
        if grep:
            selection.extend(["--grep", grep])
        if project:
            selection.extend(["--project", project])
        if selection:
            command.extend(["--", *selection])
        result = subprocess.run(command, cwd=ROOT / "frontend", env=env,
                                text=True, capture_output=True, check=False)
        # Playwright failure logs can include entered values. Always redact test credentials.
        print((result.stdout + result.stderr).replace(password, "[redacted]").replace(encoded, "[redacted]"))
        print(f"Browser screenshots: {artifacts}")
        return result.returncode
    finally:
        try:
            # Playwright error context can contain values even for password fields.
            for path in artifacts.rglob("*"):
                if path.is_file() and path.suffix in {".md", ".txt", ".json"}:
                    content = path.read_text()
                    redacted = content.replace(password, "[redacted]").replace(encoded, "[redacted]")
                    if redacted != content:
                        path.write_text(redacted)
        finally:
            for started, name in ((frontend_started, frontend_name), (api_started, api_name)):
                if started:
                    subprocess.run(["docker", "stop", name], capture_output=True, check=False)
            if created:
                python(database_sql + f"connection.cursor().execute(sql.SQL('DROP DATABASE {{}} WITH (FORCE)').format(sql.Identifier('{database}')))")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, help="Optional temporary directory for screenshots")
    parser.add_argument("--grep", help="Run only browser tests whose names match this expression")
    parser.add_argument("--individual", action="store_true", help="Use an isolated individual operator instead of demo login")
    arguments = parser.parse_args()
    # Each individual project needs its own DB: a fast focused run otherwise shares
    # one real source's 10/min login budget across both browser projects.
    projects = ("desktop", "phone") if arguments.individual else (None,)

    def checks(directory):
        return max(run(directory, arguments.grep, arguments.individual, project)
                   for project in projects)

    if arguments.artifacts:
        arguments.artifacts.mkdir(parents=True, exist_ok=True)
        result = checks(arguments.artifacts.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="leadflow-auth-browser-") as directory:
            result = checks(Path(directory))
    raise SystemExit(result)


if __name__ == "__main__":
    main()
