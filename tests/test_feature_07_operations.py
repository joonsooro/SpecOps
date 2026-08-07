from uuid import uuid4

import pytest

from specops_workflow.enums import ObservationKind, OperationStatus, ResultOutcome, System
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import (
    ExplicitFailure,
    JiraIdentity,
    NominalSuccess,
    QueryOne,
    RecordOperationResultCommand,
    RemoteObservation,
    StartExternalOperationCommand,
)

from public_api_factory import fresh_harness


def operation_fixture(tmp_path):
    harness = fresh_harness(tmp_path)
    harness.build_approved_package()
    harness.build_approved_jira_plan()
    harness.build_approved_status_policy()
    intent = next(item for item in harness.pending_intents() if item.system == System.JIRA)
    return harness, intent, intent.request["generation_key"]


def test_success_requires_matching_readback_and_confirms_identity_once(tmp_path):
    harness, intent, generation = operation_fixture(tmp_path)
    operation_id = uuid4()
    started = harness.service.start_external_operation(
        StartExternalOperationCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=harness.revision,
            operation_id=operation_id,
            intent_id=intent.intent_id,
            idempotency_key="stable-key",
        )
    )
    observation = RemoteObservation(
        observation_kind=ObservationKind.FOUND,
        system=System.JIRA,
        external_identity=JiraIdentity(key="OPS-1"),
        generation_key=generation,
        package_binding=intent.package_binding,
        plan_binding=intent.plan_binding,
        status_policy_binding=intent.status_policy_binding,
        remote_revision="r1",
        native_status="To Do",
        owned_content=intent.request,
    )
    result = harness.service.record_operation_result(
        RecordOperationResultCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=started.receipt.revision,
            operation_id=operation_id,
            attempt=1,
            outcome=NominalSuccess(
                outcome=ResultOutcome.NOMINAL_SUCCESS, read_back=observation
            ),
        )
    )
    assert result.status == OperationStatus.SUCCEEDED
    assert result.confirmation.external_identity.key == "OPS-1"
    delivery = harness.service.get_delivery_view(
        QueryOne(case_id=harness.case_id, acting_actor_id="SYSTEM")
    )
    confirmed = [item for item in delivery.items if item.external_identity is not None]
    assert len(confirmed) == 1 and confirmed[0].external_identity.key == "OPS-1"


def test_failed_retry_and_blind_retry_rules(tmp_path):
    harness, intent, _ = operation_fixture(tmp_path)
    operation_id = uuid4()
    started = harness.service.start_external_operation(
        StartExternalOperationCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=harness.revision,
            operation_id=operation_id,
            intent_id=intent.intent_id,
            idempotency_key="stable-key",
        )
    )
    with pytest.raises(DomainError) as caught:
        harness.service.start_external_operation(
            StartExternalOperationCommand(
                command_id=uuid4(),
                case_id=harness.case_id,
                acting_actor_id="SYSTEM",
                expected_case_revision=started.receipt.revision,
                operation_id=operation_id,
                intent_id=intent.intent_id,
                idempotency_key="stable-key",
            )
        )
    assert caught.value.code == ErrorCode.OPERATION_RETRY_BLOCKED
    failed = harness.service.record_operation_result(
        RecordOperationResultCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=started.receipt.revision,
            operation_id=operation_id,
            attempt=1,
            outcome=ExplicitFailure(
                outcome=ResultOutcome.EXPLICIT_FAILURE,
                failure_code="transport-failed",
            ),
        )
    )
    retried = harness.service.start_external_operation(
        StartExternalOperationCommand(
            command_id=uuid4(),
            case_id=harness.case_id,
            acting_actor_id="SYSTEM",
            expected_case_revision=failed.receipt.revision,
            operation_id=operation_id,
            intent_id=intent.intent_id,
            idempotency_key="stable-key",
        )
    )
    assert retried.status == OperationStatus.PENDING and retried.attempt == 2
