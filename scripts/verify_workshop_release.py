from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND / "frontend"
RECEIPT = BACKEND / "tests/workshop/sw-release-evidence.json"
LIVE_RECEIPT = BACKEND / "tests/workshop/live-evidence.json"
sys.path.insert(0, str(BACKEND / "tests/workshop"))
from sw_release_contract import SW_EVIDENCE, SW_IDS  # noqa: E402


def run(name: str, command: list[str], cwd: Path) -> dict[str, object]:
    completed = subprocess.run(command, cwd=cwd, check=False)
    return {"name": name, "status": "PASS" if completed.returncode == 0 else "FAIL"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run the credentialed SW-EV-024 probe")
    parser.add_argument(
        "--live-receipt",
        action="store_true",
        help="verify an existing credentialed SW-EV-024 PASS receipt without another provider call",
    )
    args = parser.parse_args()
    if args.live and args.live_receipt:
        parser.error("choose either --live or --live-receipt")
    checks = [
        run("pytest", [str(BACKEND / ".venv/bin/pytest"), "-q"], BACKEND),
        run("vitest", ["npm", "test"], FRONTEND),
        run("frontend-build", ["npm", "run", "build"], FRONTEND),
        run("playwright", ["npm", "run", "test:e2e"], FRONTEND),
    ]
    deterministic_passed = all(value["status"] == "PASS" for value in checks)
    if args.live and deterministic_passed:
        checks.append(run(
            "SW-EV-024-live",
            [str(BACKEND / ".venv/bin/python"), "tests/workshop/live_workshop_probe.py"],
            BACKEND,
        ))
    elif args.live:
        checks.append({"name": "SW-EV-024-live", "status": "NOT_RUN"})
    elif args.live_receipt:
        try:
            live = json.loads(LIVE_RECEIPT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            live = {}
        receipt_passed = (
            live.get("sw_ev") == "SW-EV-024"
            and live.get("status") == "PASS"
            and live.get("gemini_provider") == "GeminiLiveProvider"
            and live.get("terra_provider") == "TerraResponsesProvider"
            and isinstance(live.get("gemini_audio_bytes"), int)
            and live["gemini_audio_bytes"] > 0
            and isinstance(live.get("foundation_revision"), int)
            and isinstance(live.get("package_binding"), dict)
            and isinstance(live.get("decision_source_pointer"), dict)
        )
        checks.append({
            "name": "SW-EV-024-live",
            "status": "PASS" if receipt_passed else "FAIL",
        })
    live_requested = args.live or args.live_receipt
    live_passed = not live_requested or (
        LIVE_RECEIPT.is_file()
        and checks[-1]["status"] == "PASS"
    )
    status_by_kind = {
        "pytest": next(value["status"] for value in checks if value["name"] == "pytest"),
        "vitest": next(value["status"] for value in checks if value["name"] == "vitest"),
        "playwright": next(value["status"] for value in checks if value["name"] == "playwright"),
        "live": next(
            (value["status"] for value in checks if value["name"] == "SW-EV-024-live"),
            "NOT_RUN",
        ),
    }
    evidence = {
        sw_id: [
            {
                "kind": kind,
                "path": path,
                "name": name,
                "outcome": status_by_kind[kind],
            }
            for kind, path, name in SW_EVIDENCE[sw_id]
        ]
        for sw_id in SW_IDS
    }
    receipt = {
        "schema_version": 1,
        "inventory": list(SW_IDS),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "credentialed" if live_requested else "deterministic",
        "checks": checks,
        "evidence": evidence,
        "passed": deterministic_passed and live_passed,
    }
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
