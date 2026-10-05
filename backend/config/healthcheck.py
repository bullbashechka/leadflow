"""Probe local readiness with an allowed Host, without secrets or redirect loops."""

import os
from urllib.request import Request
from urllib.request import urlopen

host = os.environ["DJANGO_ALLOWED_HOSTS"].split(",")[0].strip()
port = int(os.environ.get("PORT", "8000"))
request = Request(f"http://127.0.0.1:{port}/api/health/", headers={"Host": host})
with urlopen(request, timeout=3) as response:  # noqa: S310
    if response.status != 200:
        raise SystemExit(1)
