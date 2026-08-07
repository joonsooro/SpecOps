"""Anonymous public-API example: fresh database, one blocked command, approved package."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from specops_workflow import DomainError, FrozenClock, WorkflowService, migrate
from specops_workflow.enums import ApprovalScope, Domain, SourceArtifactType
from specops_workflow.models import (
    AcceptanceCheck,
    AddParticipantCommand,
    ApproveSpecPackageCommand,
    CreateCaseCommand,
    CreateSpecPackageCommand,
    JsonPointer,
    MarkSpecPackageReadyCommand,
    QueryOne,
    RegisterSourceArtifactCommand,
    Requirement,
    SourceArtifactIdentity,
    SourceRef,
    SpecPackagePayload,
    TechnicalDecision,
)


def run(database_url: str) -> None:
    migrate(database_url)
    clock = FrozenClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
    service = WorkflowService(clock=clock, database_url=database_url)
    case_id, pm_actor_id, dev_lead_actor_id = uuid4(), uuid4(), uuid4()

    created = service.create_case(
        CreateCaseCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id=pm_actor_id,
            pm_actor_id=pm_actor_id,
            dev_lead_actor_id=dev_lead_actor_id,
        )
    )
    print("permitted_command=create_case", created.receipt.revision)

    outsider_id = uuid4()
    try:
        service.add_participant(
            AddParticipantCommand(
                command_id=uuid4(),
                case_id=case_id,
                acting_actor_id=outsider_id,
                expected_case_revision=created.receipt.revision,
                actor_id=outsider_id,
            )
        )
    except DomainError as error:
        print("blocked_command=add_participant", error.code.value)

    source_id = uuid4()
    source = service.register_source_artifact(
        RegisterSourceArtifactCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=created.receipt.revision,
            identity=SourceArtifactIdentity(
                artifact_id=source_id,
                case_id=case_id,
                type=SourceArtifactType.OTHER,
                version=1,
                media_type="application/json",
                canonical_locator="/registered/input.json",
                content_hash="0" * 64,
            ),
        )
    )
    source_ref = SourceRef(
        artifact_id=source_id,
        version=1,
        content_hash="0" * 64,
        location=JsonPointer(pointer="/units/0"),
    )
    requirement_id, decision_id = uuid4(), uuid4()
    payload = SpecPackagePayload(
        requirements=[
            Requirement(
                unit_id=requirement_id,
                statement="Record the approved outcome",
                domain=Domain.BUSINESS,
                delivery_required=True,
                source_refs=[source_ref],
            )
        ],
        technical_decisions=[
            TechnicalDecision(
                unit_id=decision_id,
                statement="Use deterministic transitions",
                domain=Domain.TECHNICAL,
                delivery_required=True,
                source_refs=[source_ref],
                provisional=False,
            )
        ],
        acceptance_checks=[
            AcceptanceCheck(
                check_id=uuid4(),
                statement="The outcome is reproducible",
                domain=Domain.CROSS_DOMAIN,
                related_unit_ids=[requirement_id, decision_id],
                source_refs=[source_ref],
            )
        ],
    )
    package_id = uuid4()
    package = service.create_spec_package(
        CreateSpecPackageCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=source.receipt.revision,
            package_id=package_id,
            content_schema_version=1,
            hash_schema_version=1,
            payload=payload,
        )
    )
    ready = service.mark_spec_package_ready(
        MarkSpecPackageReadyCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=package.receipt.revision,
            expected_artifact_id=package_id,
            expected_artifact_version=1,
            expected_artifact_hash=package.binding.semantic_hash,
        )
    )
    business = service.approve_spec_package(
        ApproveSpecPackageCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id=pm_actor_id,
            expected_case_revision=ready.receipt.revision,
            expected_artifact_id=package_id,
            expected_artifact_version=1,
            expected_artifact_hash=package.binding.semantic_hash,
            scope=ApprovalScope.BUSINESS,
        )
    )
    service.approve_spec_package(
        ApproveSpecPackageCommand(
            command_id=uuid4(),
            case_id=case_id,
            acting_actor_id=dev_lead_actor_id,
            expected_case_revision=business.receipt.revision,
            expected_artifact_id=package_id,
            expected_artifact_version=1,
            expected_artifact_hash=package.binding.semantic_hash,
            scope=ApprovalScope.TECHNICAL,
        )
    )
    view = service.get_workflow_view(
        QueryOne(case_id=case_id, acting_actor_id=pm_actor_id)
    )
    print(view.model_dump_json(indent=2))


if __name__ == "__main__":
    with TemporaryDirectory(prefix="specops-example-") as directory:
        database_path = Path(directory) / "workflow.sqlite"
        assert not database_path.exists()
        run(f"sqlite:///{database_path}")
