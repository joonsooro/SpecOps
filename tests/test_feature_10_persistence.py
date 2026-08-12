from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import inspect, text

from specops_workflow.models import AuditQuery, CreateCaseCommand, QueryOne
from specops_workflow.persistence import engine_for, metadata, migrate
from specops_workflow.ports import FrozenClock
from specops_workflow.service import WorkflowService


def test_fresh_migration_fk_schema_and_committed_reload(tmp_path):
    path = tmp_path / "new" / "workflow.sqlite"
    path.parent.mkdir()
    url = f"sqlite:///{path}"
    migrate(url)
    engine = engine_for(url)
    names = set(inspect(engine).get_table_names())
    assert set(metadata.tables) == names - {"alembic_version"}
    assert len(metadata.tables) == 39
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        triggers = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert triggers == {"audit_events_no_update", "audit_events_no_delete"}
    clock = FrozenClock(datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
    service = WorkflowService(clock=clock, database_url=url)
    case_id, pm, dev = uuid4(), uuid4(), uuid4()
    service.create_case(CreateCaseCommand(command_id=uuid4(), case_id=case_id, acting_actor_id=pm, pm_actor_id=pm, dev_lead_actor_id=dev))
    reopened = WorkflowService(clock=clock, database_url=url)
    view = reopened.get_workflow_view(QueryOne(case_id=case_id, acting_actor_id=pm))
    assert view.case_id == case_id and view.revision == 1
    assert len(reopened.list_audit_events(AuditQuery(case_id=case_id, acting_actor_id=pm)).items) == 1
