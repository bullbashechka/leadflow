"""The production entrypoint can receive its database CA from the secret store."""

import os
import re
import ssl
import subprocess
import sys
from pathlib import Path

import pytest


def prepare_certificate(environment):
    return subprocess.run(
        [sys.executable, "-m", "config.database_ca"],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def certificate_file(tmp_path):
    return tmp_path / "certs" / "database-root.pem"


def test_secret_store_ca_is_ready_for_tls_and_survives_repeated_start(certificate_file):
    bundle = Path(ssl.get_default_verify_paths().cafile).read_text()
    pem = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.S)[0]
    environment = {
        "DATABASE_CA_PEM": pem,
        "DATABASE_URL": f"postgres://u:p@db.test/db?sslmode=verify-full&sslrootcert={certificate_file}",
    }

    for _ in range(2):
        result = prepare_certificate(environment)
        assert result.returncode == 0, result.stderr
        ssl.create_default_context(cafile=certificate_file)
        assert certificate_file.read_text() == pem
        assert certificate_file.stat().st_mode & 0o777 == 0o600


def test_existing_mounted_ca_needs_no_secret_store_value(certificate_file):
    certificate_file.parent.mkdir()
    certificate_file.write_text("existing mounted certificate")
    result = prepare_certificate({"DATABASE_CA_PEM": "", "DATABASE_URL": ""})
    assert result.returncode == 0, result.stderr
    assert certificate_file.read_text() == "existing mounted certificate"


def test_invalid_pem_stops_startup_without_exposing_configuration(certificate_file):
    marker = "private-config-marker-do-not-log"
    result = prepare_certificate(
        {
            "DATABASE_CA_PEM": marker,
            "DATABASE_URL": f"postgres://u:{marker}@db.test/db?sslrootcert={certificate_file}",
        }
    )
    assert result.returncode != 0
    assert "DATABASE_CA_PEM" in result.stderr
    assert marker not in result.stdout + result.stderr
    assert not certificate_file.exists()


def test_invalid_replacement_preserves_previous_certificate(certificate_file):
    certificate_file.parent.mkdir()
    certificate_file.write_text("previous certificate")
    result = prepare_certificate(
        {
            "DATABASE_CA_PEM": "not a certificate",
            "DATABASE_URL": f"postgres://u:p@db.test/db?sslrootcert={certificate_file}",
        }
    )
    assert result.returncode != 0
    assert certificate_file.read_text() == "previous certificate"
