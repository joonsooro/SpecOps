from datetime import datetime, timezone
from uuid import uuid4

import pytest

from specops_workflow.enums import ApprovalScope, Domain, PackageState, SourceArtifactType
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    AcceptanceCheck, ApproveSpecPackageCommand, CreateCaseCommand, CreateSpecPackageCommand, JsonPointer,
    MarkSpecPackageReadyCommand, RegisterSourceArtifactCommand, Requirement, SourceArtifactIdentity, SourceRef,
    SpecPackagePayload, TechnicalDecision,
)
from specops_workflow.ports import FrozenClock
from specops_workflow.service import WorkflowService

ZERO = "0" * 64


def fixture():
    service = WorkflowService(clock=FrozenClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc)))
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    created = service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    source_id = uuid4()
    registered = service.register_source_artifact(RegisterSourceArtifactCommand(
        command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=created.receipt.revision,
        identity=SourceArtifactIdentity(artifact_id=source_id, case_id=case_id, type=SourceArtifactType.BUSINESS_SPEC, version=1, media_type="application/json", canonical_locator="/evidence/input.json", content_hash=ZERO),
    ))
    ref = SourceRef(artifact_id=source_id, version=1, content_hash=ZERO, location=JsonPointer(pointer="/requirements/0"))
    requirement_id, decision_id = uuid4(), uuid4()
    payload = SpecPackagePayload(
        requirements=[Requirement(unit_id=requirement_id, statement="Deliver exact lineage", domain=Domain.BUSINESS, delivery_required=True, source_refs=[ref])],
        technical_decisions=[TechnicalDecision(unit_id=decision_id, statement="Use deterministic hashes", domain=Domain.TECHNICAL, delivery_required=True, source_refs=[ref], provisional=False)],
        acceptance_checks=[AcceptanceCheck(check_id=uuid4(), statement="Lineage is exact", domain=Domain.CROSS_DOMAIN, related_unit_ids=[requirement_id, decision_id], source_refs=[ref])],
    )
    return service, case_id, pm, dev, registered.receipt.revision, payload


def test_package_only_ready_after_source_validation_and_both_scopes_approve():
    service, case_id, pm, dev, revision, payload = fixture()
    package_id = uuid4()
    created = service.create_spec_package(CreateSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, package_id=package_id, content_schema_version=1, hash_schema_version=1, payload=payload))
    ready = service.mark_spec_package_ready(MarkSpecPackageReadyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=created.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash))
    assert ready.state == PackageState.READY
    business = service.approve_spec_package(ApproveSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=ready.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.BUSINESS))
    assert service.get_case_state(case_id).package.current.state == PackageState.READY
    service.approve_spec_package(ApproveSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=business.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    assert service.get_case_state(case_id).package.current.state == PackageState.APPROVED


def test_wrong_scope_and_stale_binding_are_exact_failures():
    service, case_id, pm, _, revision, payload = fixture()
    package_id = uuid4()
    created = service.create_spec_package(CreateSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=revision, package_id=package_id, content_schema_version=1, hash_schema_version=1, payload=payload))
    with pytest.raises(DomainError) as caught:
        service.mark_spec_package_ready(MarkSpecPackageReadyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=created.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash="f" * 64))
    assert caught.value.code == ErrorCode.STALE_ARTIFACT_BINDING

