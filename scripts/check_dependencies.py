"""Check pinned public dependencies against PyPI and npm registry advisories."""

import concurrent.futures
import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def request_json(url, data=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"pypi.org", "registry.npmjs.org"}:
        raise ValueError("Only the public HTTPS package registries are permitted.")
    request = urllib.request.Request(  # noqa: S310 - HTTPS registry allowlist above.
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "leadflow-release-check"},
    )
    # Use the inherited proxy and trusted CA. Never retry with TLS verification disabled.
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.load(response)


def check_python(package):
    name, version = package
    url = "https://pypi.org/pypi/{}/{}/json".format(
        urllib.parse.quote(name, safe=""), urllib.parse.quote(version, safe=""),
    )
    result = request_json(url)
    if not isinstance(result, dict) or not isinstance(result.get("vulnerabilities"), list):
        raise ValueError("PyPI did not return the expected advisory response.")
    if any(not isinstance(advisory, dict) for advisory in result["vulnerabilities"]):
        raise ValueError("PyPI returned an invalid advisory.")
    return [
        {"ecosystem": "PyPI", "name": name, "version": version,
         "id": advisory["id"], "aliases": advisory.get("aliases", []),
         "fixed_in": advisory.get("fixed_in", [])}
        for advisory in result["vulnerabilities"] if not advisory.get("withdrawn")
    ]


def check_node(packages):
    result = request_json(
        "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk",
        json.dumps(packages).encode(),
    )
    if not isinstance(result, dict) or any(
        name not in packages or not isinstance(advisories, list)
        or any(not isinstance(advisory, dict) for advisory in advisories)
        for name, advisories in result.items()
    ):
        raise ValueError("npm did not return the expected advisory response.")
    return [
        {"ecosystem": "npm", "name": name, "versions_checked": packages[name],
         "id": advisory["id"], "severity": advisory["severity"],
         "vulnerable_versions": advisory["vulnerable_versions"], "url": advisory["url"]}
        for name, advisories in result.items() for advisory in advisories
    ]


def main():
    lock = tomllib.loads((ROOT / "backend/uv.lock").read_text())
    python_packages = sorted({
        (package["name"], package["version"]) for package in lock["package"]
        if package.get("source", {}).get("registry") == "https://pypi.org/simple"
    })
    npm_lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    npm_packages = {}
    for path, package in npm_lock["packages"].items():
        if path and package.get("version") and not package.get("link"):
            name = package.get("name", path.rsplit("node_modules/", 1)[-1])
            npm_packages.setdefault(name, set()).add(package["version"])
    npm_packages = {name: sorted(versions) for name, versions in sorted(npm_packages.items())}
    findings = []
    failed = []
    checked_python = 0
    checked_npm = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        checks = {executor.submit(check_python, package): "PyPI" for package in python_packages}
        checks[executor.submit(check_node, npm_packages)] = "npm"
        for future in concurrent.futures.as_completed(checks):
            ecosystem = checks[future]
            try:
                findings.extend(future.result())
                if ecosystem == "PyPI":
                    checked_python += 1
                else:
                    checked_npm = len(npm_packages)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
                # Do not leak proxy or authentication details from exception text.
                failed.append(ecosystem)
    print(json.dumps({
        "checked": {"PyPI_versions": checked_python, "npm_names": checked_npm},
        "unavailable_checks": {"PyPI": failed.count("PyPI"), "npm": failed.count("npm")},
        "findings": sorted(findings, key=lambda finding: (
            finding["ecosystem"], finding["name"], str(finding["id"]),
        )),
        "status": "incomplete" if failed else "findings" if findings else "no_known_advisories",
    }, sort_keys=True))
    return 2 if failed else 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
