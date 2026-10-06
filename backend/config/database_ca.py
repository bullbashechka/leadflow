"""Prepare the database trust file before Django production validation."""

import os
import ssl
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit


def prepare_database_ca():
    pem = os.environ.get("DATABASE_CA_PEM", "")
    if not pem:
        return

    temporary_path = None
    try:
        ssl.create_default_context(cadata=pem)
        paths = parse_qs(urlsplit(os.environ.get("DATABASE_URL", "")).query).get("sslrootcert", [])
        if len(paths) != 1 or not Path(paths[0]).is_absolute():
            raise ValueError("Missing absolute certificate destination")
        destination = Path(paths[0])
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as certificate:
            temporary_path = Path(certificate.name)
            certificate.write(pem)
            certificate.flush()
            os.fsync(certificate.fileno())
        temporary_path.replace(destination)
    except OSError, ValueError, ssl.SSLError:
        # Do not expose the PEM, connection URL, or exception context.
        sys.exit("DATABASE_CA_PEM could not prepare the DATABASE_URL sslrootcert file.")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    prepare_database_ca()
