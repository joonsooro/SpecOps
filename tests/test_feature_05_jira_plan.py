from uuid import uuid4

import pytest

from specops_workflow.enums import ApprovalScope, Domain, JiraKind, PlanState, PlanTarget
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    ApproveProjectionPlanCommand, ApproveSpecPackageCommand, CreateProjectionPlanCommand, CreateSpecPackageCommand,
    MarkSpecPackageReadyCommand, ProjectionItem, ProjectionPlanPayload, StructuredWorkBody,
)

from test_feature_04_package import fixture


def approved_case():
    service, case_id, pm, dev, revision, package_payload = fixture()
    package_id = uuid4()
    package = service.create_spec_package(CreateSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, package_id=package_id, content_schema_version=1, hash_schema_version=1, payload=package_payload))
    ready = service.mark_spec_package_ready(MarkSpecPackageReadyCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=package.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=package.binding.semantic_hash))
    business = service.approve_spec_package(ApproveSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=ready.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=package.binding.semantic_hash, scope=ApprovalScope.BUSINESS))
    technical = service.approve_spec_package(ApproveSpecPackageCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=business.receipt.revision, expected_artifact_id=package_id, expected_artifact_version=1, expected_artifact_hash=package.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    return service, case_id, pm, dev, technical.receipt.revision, package_payload, package.binding


def jira_payload(case_id, package_payload, binding, *, cycle=False):
    epic_id, story_id, task_id = uuid4(), uuid4(), uuid4()
    requirement = package_payload.requirements[0].unit_id; decision = package_payload.technical_decisions[0].unit_id
    def body(item_id, units, dependencies):
        return StructuredWorkBody(package_id=binding.artifact_id, package_version=binding.version, package_hash=binding.semantic_hash, generation_key=f"specops:{case_id}:jira:{item_id}", source_unit_ids=units, dependency_item_ids=dependencies, provisional=False)
    epic = ProjectionItem(item_id=epic_id, kind=JiraKind.EPIC, domain=Domain.CROSS_DOMAIN, title="Approved package", body=body(epic_id, [requirement, decision], []), source_unit_ids=[requirement, decision], implementation_required=False)
    story_deps = [task_id] if cycle else []
    story = ProjectionItem(item_id=story_id, kind=JiraKind.STORY, domain=Domain.BUSINESS, title="Deliver lineage", body=body(story_id, [requirement], story_deps), source_unit_ids=[requirement], parent_item_id=epic_id, dependency_item_ids=story_deps, implementation_required=True, repository="owner/repo")
    task_deps = [story_id]
    task = ProjectionItem(item_id=task_id, kind=JiraKind.TASK, domain=Domain.TECHNICAL, title="Implement hashing", body=body(task_id, [decision], task_deps), source_unit_ids=[decision], parent_item_id=epic_id, dependency_item_ids=task_deps, implementation_required=True, repository="owner/repo")
    return ProjectionPlanPayload(package_binding=binding, target=PlanTarget.JIRA, project_key="OPS", items=[task, epic, story])


def test_jira_hierarchy_coverage_and_approval_scopes():
    service, case_id, pm, dev, revision, package_payload, binding = approved_case()
    plan_id = uuid4(); payload = jira_payload(case_id, package_payload, binding)
    created = service.create_projection_plan(CreateProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, plan_id=plan_id, content_schema_version=1, hash_schema_version=1, payload=payload))
    assert created.state == PlanState.READY
    business = service.approve_projection_plan(ApproveProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=created.receipt.revision, expected_artifact_id=plan_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.BUSINESS))
    service.approve_projection_plan(ApproveProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=business.receipt.revision, expected_artifact_id=plan_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    assert service.get_case_state(case_id).plans[PlanTarget.JIRA].current.state == PlanState.APPROVED


def test_dependency_cycle_rejected_without_revision():
    service, case_id, _, _, revision, package_payload, binding = approved_case()
    before = service.get_case_state(case_id).revision
    with pytest.raises(DomainError) as caught:
        service.create_projection_plan(CreateProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, plan_id=uuid4(), content_schema_version=1, hash_schema_version=1, payload=jira_payload(case_id, package_payload, binding, cycle=True)))
    assert caught.value.code == ErrorCode.INVALID_PROJECTION_PLAN
    assert service.get_case_state(case_id).revision == before

