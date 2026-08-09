from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from specops_workflow import FrozenClock, WorkflowService, migrate
from specops_workflow.enums import (
    ApprovalScope,
    ArtifactKind,
    Domain,
    ItemReadiness,
    PackageReadiness,
    ReviewObligation,
    ReviewRequestKind,
    ReviewRequestStatus,
    SourceArtifactType,
)
from specops_workflow.models import (
    AcceptanceCheck,
    ApproveSpecPackageItemCommand,
    CreateCaseCommand,
    CreateReviewRequestCommand,
    CreateSpecPackageV2Command,
    GrantDelegationCommand,
    ItemBinding,
    LineRange,
    MarkSpecPackageItemReadyCommand,
    QueryOne,
    RegisterSourceArtifactCommand,
    Requirement,
    ResolveReviewRequestCommand,
    ReviseSpecPackageV2Command,
    SourceArtifactIdentity,
    SourceRef,
    SpecPackageItem,
    SpecPackagePayloadV2,
    TechnicalDecision,
    UUIDListQuery,
)

NOW = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
SOURCE_HASH = "1" * 64


def setup_service(database_url: str | None = None):
    service = WorkflowService(clock=FrozenClock(NOW), database_url=database_url)
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    created = service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    source_id = uuid4()
    registered = service.register_source_artifact(RegisterSourceArtifactCommand(
        command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=created.receipt.revision,
        identity=SourceArtifactIdentity(
            artifact_id=source_id, case_id=case_id, type=SourceArtifactType.OTHER, version=1,
            media_type="text/plain", canonical_locator="/workshop/source.txt", content_hash=SOURCE_HASH,
        ),
    ))
    ref = SourceRef(artifact_id=source_id, version=1, content_hash=SOURCE_HASH, location=LineRange(start=1, end=1))
    return service, case_id, pm, dev, registered.receipt.revision, ref


def two_item_payload(ref: SourceRef, *, technical_statement: str = "Use bounded asynchronous generation"):
    business_requirement, technical_requirement, decision_id = uuid4(), uuid4(), uuid4()
    business_check, technical_check = uuid4(), uuid4()
    business_item, technical_item = uuid4(), uuid4()
    payload = SpecPackagePayloadV2(
        requirements=[
            Requirement(unit_id=business_requirement, statement="Export every filtered order", domain=Domain.BUSINESS, delivery_required=True, source_refs=[ref]),
            Requirement(unit_id=technical_requirement, statement="Keep generation bounded", domain=Domain.TECHNICAL, delivery_required=True, source_refs=[ref]),
        ],
        technical_decisions=[
            TechnicalDecision(unit_id=decision_id, statement=technical_statement, domain=Domain.TECHNICAL, delivery_required=True, source_refs=[ref], provisional=False),
        ],
        acceptance_checks=[
            AcceptanceCheck(check_id=business_check, statement="Export count matches filters", domain=Domain.BUSINESS, related_unit_ids=[business_requirement], source_refs=[ref]),
            AcceptanceCheck(check_id=technical_check, statement="Large exports do not truncate", domain=Domain.TECHNICAL, related_unit_ids=[technical_requirement, decision_id], source_refs=[ref]),
        ],
        items=[
            SpecPackageItem(item_id=business_item, title="Filter fidelity", requirement_ids=[business_requirement], acceptance_check_ids=[business_check]),
            SpecPackageItem(item_id=technical_item, title="Large-export processing", requirement_ids=[technical_requirement], technical_decision_ids=[decision_id], acceptance_check_ids=[technical_check], dependency_item_ids=[business_item]),
        ],
    )
    return payload, business_item, technical_item


def create_v2(service, case_id, revision, payload):
    package_id = uuid4()
    result = service.create_spec_package(CreateSpecPackageV2Command(
        command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision,
        package_id=package_id, content_schema_version=2, hash_schema_version=3, payload=payload,
    ))
    return package_id, result


def item_command(model, *, case_id, actor, revision, package_binding, item_binding, **values):
    return model(
        command_id=uuid4(), case_id=case_id, acting_actor_id=actor, expected_case_revision=revision,
        expected_artifact_id=package_binding.artifact_id, expected_artifact_version=package_binding.version,
        expected_artifact_hash=package_binding.semantic_hash, item_binding=item_binding, **values,
    )


def test_v2_ownership_is_closed_and_item_hashes_are_order_independent():
    service, case_id, _, _, revision, ref = setup_service()
    payload, business_item, _ = two_item_payload(ref)
    normalized = service._normalized_package_payload(payload)
    reversed_payload = payload.model_copy(update={"items": list(reversed(payload.items)), "requirements": list(reversed(payload.requirements)), "acceptance_checks": list(reversed(payload.acceptance_checks))})
    assert service.registry.hash(ArtifactKind.SPEC_PACKAGE, 2, 3, normalized) == service.registry.hash(
        ArtifactKind.SPEC_PACKAGE, 2, 3, service._normalized_package_payload(reversed_payload)
    )
    bad_items = [item.model_copy() for item in payload.items]
    bad_items[1] = bad_items[1].model_copy(update={"requirement_ids": [*bad_items[1].requirement_ids, payload.items[0].requirement_ids[0]]})
    with pytest.raises(ValidationError):
        SpecPackagePayloadV2(**{**payload.model_dump(mode="python"), "items": bad_items})
    _, created = create_v2(service, case_id, revision, payload)
    view = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id="SYSTEM"))
    assert view.package_readiness == PackageReadiness.FORMULATING
    assert next(item for item in view.items if item.binding.item_id == business_item).binding.item_version == 1
    assert created.binding.version == 1


def test_partial_readiness_selective_revision_and_restart(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'items.sqlite'}"
    migrate(database_url)
    service, case_id, pm, dev, revision, ref = setup_service(database_url)
    payload, business_id, technical_id = two_item_payload(ref)
    package_id, created = create_v2(service, case_id, revision, payload)
    view = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm))
    by_id = {item.binding.item_id: item for item in view.items}
    business_binding, technical_binding = by_id[business_id].binding, by_id[technical_id].binding
    marked_business = service.mark_spec_package_item_ready(item_command(
        MarkSpecPackageItemReadyCommand, case_id=case_id, actor=pm, revision=created.receipt.revision,
        package_binding=created.binding, item_binding=business_binding,
    ))
    approved_business = service.approve_spec_package_item(item_command(
        ApproveSpecPackageItemCommand, case_id=case_id, actor=pm, revision=marked_business.receipt.revision,
        package_binding=created.binding, item_binding=business_binding, scope=ApprovalScope.BUSINESS,
    ))
    marked_technical = service.mark_spec_package_item_ready(item_command(
        MarkSpecPackageItemReadyCommand, case_id=case_id, actor=pm, revision=approved_business.receipt.revision,
        package_binding=created.binding, item_binding=technical_binding,
    ))
    review_id = uuid4()
    requested = service.create_review_request(item_command(
        CreateReviewRequestCommand, case_id=case_id, actor=pm, revision=marked_technical.receipt.revision,
        package_binding=created.binding, item_binding=technical_binding, review_request_id=review_id,
        kind=ReviewRequestKind.DECISION_REQUIRED, question="What measured limit governs asynchronous generation?",
        evidence_refs=[ref], attempted_resolution="The available draft does not provide measured capacity.", reviewer_actor_ids=[dev],
    ))
    partial = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm))
    assert partial.package_readiness == PackageReadiness.PARTIALLY_READY
    assert {item.binding.item_id: item.readiness for item in partial.items} == {business_id: ItemReadiness.READY, technical_id: ItemReadiness.BLOCKED}

    replacement = payload.model_copy(deep=True)
    replacement.technical_decisions[0].statement = "Use a measured 10,000-row asynchronous threshold"
    revised = service.revise_spec_package(ReviseSpecPackageV2Command(
        command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=requested.receipt.revision,
        expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash,
        content_schema_version=2, hash_schema_version=3, payload=replacement,
    ))
    after_revision = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm))
    revised_by_id = {item.binding.item_id: item for item in after_revision.items}
    assert revised_by_id[business_id].binding == business_binding
    assert revised_by_id[business_id].readiness == ItemReadiness.READY
    assert revised_by_id[technical_id].binding.item_version == 2
    marked_new = service.mark_spec_package_item_ready(item_command(
        MarkSpecPackageItemReadyCommand, case_id=case_id, actor=pm, revision=revised.receipt.revision,
        package_binding=revised.binding, item_binding=revised_by_id[technical_id].binding,
    ))
    approved_new = service.approve_spec_package_item(item_command(
        ApproveSpecPackageItemCommand, case_id=case_id, actor=dev, revision=marked_new.receipt.revision,
        package_binding=revised.binding, item_binding=revised_by_id[technical_id].binding, scope=ApprovalScope.TECHNICAL,
    ))
    resolved = service.resolve_review_request(item_command(
        ResolveReviewRequestCommand, case_id=case_id, actor=dev, revision=approved_new.receipt.revision,
        package_binding=revised.binding, item_binding=revised_by_id[technical_id].binding,
        review_request_id=review_id, resolution_text="Measured threshold accepted.", resolution_source_refs=[ref],
    ))
    assert resolved.review_request.status == ReviewRequestStatus.RESOLVED
    final = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm))
    assert final.package_readiness == PackageReadiness.READY
    reopened = WorkflowService(clock=FrozenClock(NOW), database_url=database_url)
    assert reopened.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm)) == final
    assert reopened.list_review_requests(UUIDListQuery(case_id=case_id, acting_actor_id=pm)).items == [resolved.review_request]


def test_delegated_cross_domain_item_is_ready_with_later_review_until_direct_approval():
    service, case_id, pm, dev, revision, ref = setup_service()
    requirement_id, decision_id, check_id, item_id = uuid4(), uuid4(), uuid4(), uuid4()
    payload = SpecPackagePayloadV2(
        requirements=[Requirement(unit_id=requirement_id, statement="Export the full filtered set", domain=Domain.BUSINESS, delivery_required=True, source_refs=[ref])],
        technical_decisions=[TechnicalDecision(unit_id=decision_id, statement="Generate large exports asynchronously", domain=Domain.TECHNICAL, delivery_required=True, source_refs=[ref], provisional=False)],
        acceptance_checks=[AcceptanceCheck(check_id=check_id, statement="No result is truncated", domain=Domain.CROSS_DOMAIN, related_unit_ids=[requirement_id, decision_id], source_refs=[ref])],
        items=[SpecPackageItem(item_id=item_id, title="Complete export", requirement_ids=[requirement_id], technical_decision_ids=[decision_id], acceptance_check_ids=[check_id])],
    )
    package_id, created = create_v2(service, case_id, revision, payload)
    delegation = service.grant_delegation(GrantDelegationCommand(
        command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=created.receipt.revision,
        delegation_id=uuid4(), delegate_id=pm, domain=Domain.TECHNICAL, command_names=["approve_spec_package_item"],
        artifact_kind=ArtifactKind.SPEC_PACKAGE, artifact_id=package_id, valid_from=NOW - timedelta(days=1), valid_until=NOW + timedelta(days=1), later_review_required=True,
    ))
    binding = service.get_spec_package_governance(QueryOne(case_id=case_id, acting_actor_id=pm)).items[0].binding
    marked = service.mark_spec_package_item_ready(item_command(
        MarkSpecPackageItemReadyCommand, case_id=case_id, actor=pm, revision=delegation.receipt.revision,
        package_binding=created.binding, item_binding=binding,
    ))
    business = service.approve_spec_package_item(item_command(
        ApproveSpecPackageItemCommand, case_id=case_id, actor=pm, revision=marked.receipt.revision,
        package_binding=created.binding, item_binding=binding, scope=ApprovalScope.BUSINESS,
    ))
    delegated = service.approve_spec_package_item(item_command(
        ApproveSpecPackageItemCommand, case_id=case_id, actor=pm, revision=business.receipt.revision,
        package_binding=created.binding, item_binding=binding, scope=ApprovalScope.TECHNICAL,
    ))
    assert delegated.readiness == ItemReadiness.READY and delegated.review_obligation == ReviewObligation.LATER_REVIEW
    direct = service.approve_spec_package_item(item_command(
        ApproveSpecPackageItemCommand, case_id=case_id, actor=dev, revision=delegated.receipt.revision,
        package_binding=created.binding, item_binding=binding, scope=ApprovalScope.TECHNICAL,
    ))
    assert direct.readiness == ItemReadiness.READY and direct.review_obligation == ReviewObligation.NONE
    assert service.list_review_requests(UUIDListQuery(case_id=case_id, acting_actor_id=pm)).items[0].status == ReviewRequestStatus.RESOLVED
