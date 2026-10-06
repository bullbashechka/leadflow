"""Apply a reviewed service manifest through the supported Railway API CLI."""

import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = json.loads(args.manifest.read_text())
    variables = {
        "serviceId": args.service_id,
        "environmentId": args.environment_id,
        "input": settings,
    }
    if args.dry_run:
        print(json.dumps(variables, indent=2))
        return
    query = """mutation ConfigureService(
        $serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!
    ) {
        serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input)
    }"""
    subprocess.run(
        ["railway", "api", query, "--variables", "@-", "--compact"],
        input=json.dumps(variables),
        text=True,
        check=True,
    )


if __name__ == "__main__":
    main()
