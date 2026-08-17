from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND / "frontend"
# Task 26 and Task 27 receipts remain immutable. Task 28 writes only this
# additive deterministic receipt and deliberately has no live-provider mode.
RECEIPT = BACKEND / "tests/workshop/task28-deterministic-evidence.json"
sys.path.insert(0, str(BACKEND / "tests/workshop"))
from sw_release_contract import SW_EVIDENCE  # noqa: E402


TASK28_SW_IDS = (
    "SW-EV-001",
    "SW-EV-005",
    "SW-EV-006",
    "SW-EV-007",
    "SW-EV-008",
    "SW-EV-009",
    "SW-EV-011",
    "SW-EV-012",
    "SW-EV-013",
    "SW-EV-016",
    "SW-EV-018",
    "SW-EV-019",
    "SW-EV-020",
    "SW-EV-021",
    "SW-EV-022",
    "SW-EV-023",
)
V0R_IDS = tuple(f"V0R-EV-{index:03d}" for index in range(1, 17))
AR_IDS = ("AR-EV-011", "AR-EV-012", "AR-EV-013")

V0R_EVIDENCE = {
    "V0R-EV-001": ("playwright", "frontend/e2e/workshop.spec.ts", "renders canonical question messages and submits the exact typed response intent"),
    "V0R-EV-002": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_bootstrap_runway_is_foundation_admitted_only_at_exact_one_plus_three"),
    "V0R-EV-003": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_unsafe_bootstrap_runway_fails_closed_without_guidance"),
    "V0R-EV-004": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_delayed_projection_uses_server_time_and_exact_copy"),
    "V0R-EV-005": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_bootstrap_runway_is_foundation_admitted_only_at_exact_one_plus_three"),
    "V0R-EV-006": ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_luna_selection_is_materialized_from_exact_supplied_foundation_questions"),
    "V0R-EV-007": ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_failure_rolls_back_snapshot_evidence_consumption_and_job"),
    "V0R-EV-008": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_presence_websocket_starts_grace_only_after_last_browser_page_closes"),
    "V0R-EV-009": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_cancelled_analysis_lease_reclaims_provider_completed_stage_without_another_call"),
    "V0R-EV-010": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_depth_two_enqueues_one_guidance_and_turn_jobs_remain_first"),
    "V0R-EV-011": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_depth_two_enqueues_one_guidance_and_turn_jobs_remain_first"),
    "V0R-EV-012": ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_openai_chatbot_provider_is_luna_medium_stateless_and_selection_only"),
    "V0R-EV-013": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_zero_runway_instruction_is_fixed_and_never_invents_a_question"),
    "V0R-EV-014": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_cleanup_clears_only_confirmed_ids_then_retries_same_uncertain_id"),
    "V0R-EV-015": ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_ingress_idempotency_correction_and_channel_provenance_survive_restart"),
    "V0R-EV-016": ("pytest", "tests/workshop/test_feature_27_v0_preparation_runtime.py", "test_verified_turn_branch_replenishes_before_one_bounded_correction"),
}

AR_EVIDENCE = {
    "AR-EV-011": ("pytest", "tests/workshop/test_feature_20_release.py", "test_operational_telemetry_redacts_and_secrets_stay_opaque"),
    "AR-EV-012": ("playwright", "frontend/e2e/workshop.spec.ts", "renders canonical question messages and submits the exact typed response intent"),
    "AR-EV-013": ("pytest", "tests/workshop/test_feature_28_text_authoritative_workshop.py", "test_visual_edit_and_reject_are_explicit_idempotent_proposal_actions"),
}


def run(
    name: str,
    command: list[str],
    cwd: Path,
    *,
    unset_environment: tuple[str, ...] = (),
) -> dict[str, str]:
    environment = os.environ.copy()
    for key in unset_environment:
        environment.pop(key, None)
    completed = subprocess.run(command, cwd=cwd, check=False, env=environment)
    return {"name": name, "status": "PASS" if completed.returncode == 0 else "FAIL"}


def main() -> int:
    checks = [
        run(
            "pytest",
            [sys.executable, "-m", "pytest", "-q"],
            BACKEND,
            unset_environment=(
                "SPECOPS_DATABASE_URL",
                "WORKSHOP_DATABASE_URL",
                "OPENAI_API_KEY",
                "GEMINI_API_KEY",
            ),
        ),
        run("vitest", ["npm", "test"], FRONTEND),
        run("frontend-build", ["npm", "run", "build"], FRONTEND),
        run("playwright", ["npm", "run", "test:e2e"], FRONTEND),
        run("playwright-real", ["npm", "run", "test:e2e:real"], FRONTEND),
    ]
    status = {item["name"]: item["status"] for item in checks}
    status_by_kind = {
        "pytest": status["pytest"],
        "vitest": status["vitest"],
        "playwright": status["playwright"],
    }
    evidence: dict[str, list[dict[str, str]]] = {}
    for evidence_id in TASK28_SW_IDS:
        evidence[evidence_id] = [
            {
                "kind": kind,
                "path": path,
                "name": name,
                "outcome": status_by_kind[kind],
            }
            for kind, path, name in SW_EVIDENCE[evidence_id]
        ]
    for evidence_id, (kind, path, name) in {**V0R_EVIDENCE, **AR_EVIDENCE}.items():
        evidence[evidence_id] = [{
            "kind": kind,
            "path": path,
            "name": name,
            "outcome": status_by_kind[kind],
        }]
    passed = all(item["status"] == "PASS" for item in checks)
    receipt = {
        "schema_version": 1,
        "task": 28,
        "inventory": [*TASK28_SW_IDS, *V0R_IDS, *AR_IDS],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "deterministic-no-provider-calls",
        "checks": checks,
        "evidence": evidence,
        "excluded_independent_evaluation": "SW-EV-024",
        "passed": passed,
    }
    RECEIPT.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
