"""Set a local demo hash without printing credentials or using shell interpolation."""

import getpass
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    password = getpass.getpass("New local CRM demo password: ")
    if not password or len(password) > 1024:
        raise SystemExit("Use a non-empty password of at most 1024 characters.")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match; no configuration changed.")
    target = ROOT / ".env"
    if not target.exists():
        raise SystemExit("Run python3 scripts/init_local_env.py first.")
    result = subprocess.run(
        ["docker", "compose", "run", "--rm", "-T", "--no-deps", "api", "python", "-c",
         "import sys; from django.contrib.auth.hashers import make_password; "
         "print(make_password(sys.stdin.read()))"],
        input=password, text=True, capture_output=True, cwd=ROOT, check=False,
    )
    if result.returncode:
        raise SystemExit("Hash generation failed; check the local Docker environment.")
    encoded = result.stdout.strip()
    if not encoded.startswith("argon2$") or "\n" in encoded or "'" in encoded:
        raise SystemExit("Unexpected hash format; no configuration changed.")
    lines = target.read_text().splitlines()
    lines = [line for line in lines if not line.startswith("CRM_DEMO_PASSWORD_HASH=")]
    # Single quotes prevent Compose from interpolating the dollar signs in the hash.
    lines.append(f"CRM_DEMO_PASSWORD_HASH='{encoded}'")
    if not any(line.startswith("DJANGO_CSRF_TRUSTED_ORIGINS=") for line in lines):
        port = next((line.split("=", 1)[1] for line in lines if line.startswith("FRONTEND_PORT=")), "5173")
        if not port.isdigit():
            raise SystemExit("FRONTEND_PORT must be numeric; no configuration changed.")
        lines.append(f"DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:{port},http://127.0.0.1:{port}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Local demo hash updated. Recreate API: docker compose up -d --no-deps --force-recreate api")


if __name__ == "__main__":
    main()
