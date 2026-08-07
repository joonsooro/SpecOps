from uuid import uuid4

import pytest

from specops_workflow.enums import Domain, GitHubKind, PlanTarget, SetupStage, System
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    CreateProjectionPlanCommand,
    JiraIdentity,
    ProjectionItem,
    ProjectionPlanPayload,
    QueryOne,
    StructuredWorkBody,
)
from specops_workflow.renderer import render_structured_body

from public_api_factory import fresh_harness


def with_confirmed_jira(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    harness.apply_jira()
    return harness


def test_fixed_renderer_and_exact_github_leaf_coverage(tmp_path):
    harness = with_confirmed_jira(tmp_path)
    created = harness.build_approved_github_plan()
    payload = harness.github_payload
    rendered = render_structured_body(payload.items[0].body, {})
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert "Acceptance:\n" in rendered
    assert "Dependencies:\n- NONE\n" in rendered
    view = harness.service.get_workflow_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    assert view.current_github_plan == created
    assert view.setup_stage == SetupStage.GITHUB_APPLY
    assert len([item for item in harness.pending_intents() if item.system == System.GITHUB]) == 1


def test_missing_implementation_leaf_is_rejected(tmp_path):
    harness = with_confirmed_jira(tmp_path)
    epic = next(item for item in harness.jira_payload.items if not item.implementation_required)
    delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    epic_delivery = next(item for item in delivery.items if item.item_id == epic.item_id)
    assert isinstance(epic_delivery.external_identity, JiraIdentity)
    item_id = uuid4()
    body = StructuredWorkBody(
        package_id=harness.package_binding.artifact_id,
        package_version=harness.package_binding.version,
        package_hash=harness.package_binding.semantic_hash,
        generation_key=f"specops:{harness.case_id}:github:{item_id}",
        source_unit_ids=epic.source_unit_ids,
        acceptance_checks=epic.body.acceptance_checks,
        provisional=False,
        jira_key=epic_delivery.external_identity.key,
    )
    invalid = ProjectionPlanPayload(
        package_binding=harness.package_binding,
        target=PlanTarget.GITHUB,
        jira_plan_binding=harness.jira_binding,
        items=[
            ProjectionItem(
                item_id=item_id,
                kind=GitHubKind.ISSUE,
                domain=Domain.CROSS_DOMAIN,
                title="Invalid nonimplementation mapping",
                body=body,
                source_unit_ids=epic.source_unit_ids,
                implementation_required=False,
                repository="local/workflow",
                primary_jira_item_id=epic.item_id,
            )
        ],
    )
    before = harness.revision
    with pytest.raises(DomainError) as caught:
        harness.service.create_projection_plan(
            CreateProjectionPlanCommand(
                command_id=uuid4(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=before,
                plan_id=uuid4(),
                content_schema_version=1,
                hash_schema_version=1,
                payload=invalid,
            )
        )
    assert caught.value.code == ErrorCode.INVALID_PROJECTION_PLAN
    assert harness.revision == before
