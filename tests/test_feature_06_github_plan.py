from uuid import uuid4

import pytest

from specops_workflow.enums import ApprovalScope, Domain, GitHubKind, PlanState, PlanTarget
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import ApproveProjectionPlanCommand, CreateProjectionPlanCommand, ProjectionItem, ProjectionPlanPayload, StructuredWorkBody
from specops_workflow.renderer import render_structured_body

from test_feature_05_jira_plan import approved_case, jira_payload


def with_jira():
    service, case_id, pm, dev, revision, package_payload, package_binding = approved_case()
    jira_id = uuid4(); jp = jira_payload(case_id, package_payload, package_binding)
    created = service.create_projection_plan(CreateProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, plan_id=jira_id, content_schema_version=1, hash_schema_version=1, payload=jp))
    business = service.approve_projection_plan(ApproveProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, expected_case_revision=created.receipt.revision, expected_artifact_id=jira_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.BUSINESS))
    technical = service.approve_projection_plan(ApproveProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=business.receipt.revision, expected_artifact_id=jira_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    implementation = [item for item in jp.items if item.implementation_required]
    service.get_case_state(case_id).metadata["external_bindings"] = {item.item_id: {"key": f"OPS-{index + 1}"} for index, item in enumerate(implementation)}
    return service, case_id, dev, technical.receipt.revision, package_binding, created.binding, implementation


def github_payload(case_id, package_binding, jira_binding, implementation, *, omit_last=False):
    items = []
    for primary in implementation[:-1] if omit_last else implementation:
        item_id = uuid4(); key = f"OPS-{implementation.index(primary) + 1}"
        body = StructuredWorkBody(package_id=package_binding.artifact_id, package_version=package_binding.version, package_hash=package_binding.semantic_hash, generation_key=f"specops:{case_id}:github:{item_id}", source_unit_ids=primary.source_unit_ids, provisional=False, jira_key=key)
        items.append(ProjectionItem(item_id=item_id, kind=GitHubKind.ISSUE, domain=primary.domain, title=primary.title, body=body, source_unit_ids=primary.source_unit_ids, implementation_required=False, repository=primary.repository, primary_jira_item_id=primary.item_id))
    return ProjectionPlanPayload(package_binding=package_binding, target=PlanTarget.GITHUB, jira_plan_binding=jira_binding, items=items)


def test_fixed_renderer_and_exact_github_leaf_coverage():
    service, case_id, dev, revision, package_binding, jira_binding, implementation = with_jira()
    payload = github_payload(case_id, package_binding, jira_binding, implementation)
    rendered = render_structured_body(payload.items[0].body, {})
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert "Acceptance:\n- NONE\nDependencies:\n- NONE\n" in rendered
    plan_id = uuid4()
    created = service.create_projection_plan(CreateProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, plan_id=plan_id, content_schema_version=1, hash_schema_version=1, payload=payload))
    service.approve_projection_plan(ApproveProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=dev, expected_case_revision=created.receipt.revision, expected_artifact_id=plan_id, expected_artifact_version=1, expected_artifact_hash=created.binding.semantic_hash, scope=ApprovalScope.TECHNICAL))
    assert service.get_case_state(case_id).plans[PlanTarget.GITHUB].current.state == PlanState.APPROVED


def test_missing_implementation_leaf_is_rejected():
    service, case_id, _, revision, package_binding, jira_binding, implementation = with_jira()
    with pytest.raises(DomainError) as caught:
        service.create_projection_plan(CreateProjectionPlanCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=revision, plan_id=uuid4(), content_schema_version=1, hash_schema_version=1, payload=github_payload(case_id, package_binding, jira_binding, implementation, omit_last=True)))
    assert caught.value.code == ErrorCode.INVALID_PROJECTION_PLAN

