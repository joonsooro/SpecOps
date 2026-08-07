from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from specops_workflow.enums import SourceArtifactType
from specops_workflow.models import AuditQuery, CreateCaseCommand, RegisterSourceArtifactCommand, SourceArtifactIdentity
from specops_workflow.persistence import engine_for, migrate
from specops_workflow.ports import FrozenClock
from specops_workflow.service import WorkflowService


def persisted(tmp_path):
    url = f"sqlite:///{tmp_path / 'audit.sqlite'}"; migrate(url)
    clock = FrozenClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc)); service = WorkflowService(clock=clock, database_url=url)
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    created = service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    source_id = uuid4()
    service.register_source_artifact(RegisterSourceArtifactCommand(command_id=uuid4(), case_id=case_id, acting_actor_id="SYSTEM", expected_case_revision=created.receipt.revision, identity=SourceArtifactIdentity(artifact_id=source_id, case_id=case_id, type=SourceArtifactType.OTHER, version=1, media_type="application/json", canonical_locator="/source.json", content_hash="0" * 64)))
    return url, clock, case_id, source_id


def test_audit_rows_are_append_only(tmp_path):
    url, _, _, _ = persisted(tmp_path); engine = engine_for(url)
    with pytest.raises(DatabaseError, match="AUDIT_APPEND_ONLY"):
        with engine.begin() as connection: connection.execute(text("UPDATE audit_events SET command_name='changed'"))
    with pytest.raises(DatabaseError, match="AUDIT_APPEND_ONLY"):
        with engine.begin() as connection: connection.execute(text("DELETE FROM audit_events"))


def test_reload_hydrates_source_through_pydantic_contract(tmp_path):
    url, clock, case_id, source_id = persisted(tmp_path)
    reopened = WorkflowService(clock=clock, database_url=url)
    events = reopened.list_audit_events(AuditQuery(case_id=case_id, acting_actor_id="SYSTEM"))
    source = next(item.result["identity"] for item in events.items if item.command_name == "register_source_artifact")
    assert source["artifact_id"] == str(source_id)
    assert source["case_id"] == str(case_id)
    assert source["registered_at"] == clock.now().isoformat().replace("+00:00", "Z")
