from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from specops_workshop.v4.api import create_contract_app


ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "src/specops_workshop/v4/generated/openapi/workshop-v1.openapi.json"
TYPESCRIPT = ROOT / "frontend/src/generated/workshopProtocol.ts"


def _openapi_text() -> str:
    return json.dumps(create_contract_app().openapi(), indent=2, sort_keys=True) + "\n"


def _generate_typescript() -> str:
    completed = subprocess.run(
        [
            str(ROOT / "frontend/node_modules/.bin/openapi-typescript"),
            str(OPENAPI),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected_openapi = _openapi_text()
    if args.check:
        if not OPENAPI.is_file() or OPENAPI.read_text(encoding="utf-8") != expected_openapi:
            raise SystemExit("generated Workshop OpenAPI is stale")
        expected_typescript = _generate_typescript()
        if not TYPESCRIPT.is_file() or TYPESCRIPT.read_text(encoding="utf-8") != expected_typescript:
            raise SystemExit("generated Workshop TypeScript contract is stale")
        return 0
    OPENAPI.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI.write_text(expected_openapi, encoding="utf-8")
    TYPESCRIPT.parent.mkdir(parents=True, exist_ok=True)
    TYPESCRIPT.write_text(_generate_typescript(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
