"""Development defaults must not override an explicit authentication mode."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", [None, "demo", "individual"])
def test_local_authentication_mode_respects_environment(mode):
    environment = os.environ.copy()
    environment.pop("CRM_AUTH_MODE", None)
    if mode is not None:
        environment["CRM_AUTH_MODE"] = mode
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from config.settings.local import CRM_AUTH_MODE; print(CRM_AUTH_MODE)",
        ],
        env=environment,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "Local settings could not be imported."
    assert result.stdout.strip() == (mode or "demo")
