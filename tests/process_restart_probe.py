from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

from specops_workflow import WorkflowService
from specops_workflow.models import OperationAttemptQuery, UUIDListQuery

from public_api_factory import fresh_harness


def public_evidence(service: WorkflowService, case_id: UUID) -> dict[str, object]:
    approvals = service.list_approval_records(
        UUIDListQuery(case_id=case_id, acting_actor_id="SYSTEM", limit=500)
    )
    attempts = service.list_external_operation_attempts(
        OperationAttemptQuery(case_id=case_id, acting_actor_id="SYSTEM", limit=500)
    )
    return {
        "approvals": approvals.model_dump(mode="json"),
        "attempts": attempts.model_dump(mode="json"),
    }


def main() -> None:
    mode = sys.argv[1]
    if mode == "write":
        harness = fresh_harness(Path(sys.argv[2])).complete()
        result = {
            "database_url": harness.database_url,
            "case_id": str(harness.case_id),
            **public_evidence(harness.service, harness.case_id),
        }
    elif mode == "read":
        database_url, case_id = sys.argv[2], UUID(sys.argv[3])
        result = public_evidence(WorkflowService(database_url=database_url), case_id)
    else:
        raise SystemExit(f"unknown mode: {mode}")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
