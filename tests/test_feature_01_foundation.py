from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from specops_workflow.errors import ErrorCode
from specops_workflow.models import COMMAND_MODELS, CreateCaseCommand, QueryOne
from specops_workflow.ports import FrozenClock, SequenceIdGenerator


def test_all_21_commands_are_strict_models():
    assert len(COMMAND_MODELS) == 21
    assert all(model.model_config["extra"] == "forbid" for model in COMMAND_MODELS.values())


def test_closed_domain_error_inventory():
    assert len(ErrorCode) == 22
    assert ErrorCode.UNKNOWN_EXTERNAL_RESULT.value == "UNKNOWN_EXTERNAL_RESULT"


def test_create_case_contract_and_system_query_boundary():
    command = CreateCaseCommand(
        command_id=uuid4(), case_id=uuid4(), acting_actor_id=uuid4(), pm_actor_id=uuid4(), dev_lead_actor_id=uuid4()
    )
    assert command.command_id
    assert QueryOne(case_id=uuid4(), acting_actor_id="SYSTEM").acting_actor_id == "SYSTEM"
    with pytest.raises(ValidationError):
        CreateCaseCommand.model_validate({**command.model_dump(), "unexpected": True})


def test_injected_clock_and_ids_are_deterministic():
    instant = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    value = UUID("00000000-0000-4000-8000-000000000001")
    assert FrozenClock(instant).now() == instant
    assert SequenceIdGenerator([value]).new() == value

