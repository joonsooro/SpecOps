from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

import pytest

from specops_contracts.migrations import (
    MigrationRebindingRequired,
    migrate_legacy_readiness,
    require_unique_historical_binding,
)
from specops_workflow import migrate
from specops_workflow.persistence import engine_for
from specops_workflow.workshop_protocol_storage import V4_TABLE_NAMES
from specops_workflow.artifact_quality_storage import (
    ARTIFACT_QUALITY_TABLE_NAMES,
    EVIDENCE_ASSESSMENT_TABLE_NAMES,
)


def _alembic(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_0002_database_upgrades_to_0003_with_exact_v4_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    migrate(url)
    command.downgrade(_alembic(url), "0002")
    assert not set(V4_TABLE_NAMES).intersection(inspect(engine_for(url)).get_table_names())
    migrate(url)
    inspector = inspect(engine_for(url))
    assert set(V4_TABLE_NAMES).issubset(inspector.get_table_names())
    assert "confirmed_case_revision" in {
        column["name"] for column in inspector.get_columns("workshop_artifact_confirmations")
    }
    with engine_for(url).connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
    assert set(ARTIFACT_QUALITY_TABLE_NAMES).issubset(inspector.get_table_names())
    assert set(EVIDENCE_ASSESSMENT_TABLE_NAMES).issubset(inspector.get_table_names())


def test_0005_database_adds_provider_resource_lifecycle_without_rebuild(tmp_path):
    url = f"sqlite:///{tmp_path / 'provider-lifecycle.sqlite'}"
    migrate(url)
    command.downgrade(_alembic(url), "0005")
    before = {
        item["name"] for item in inspect(engine_for(url)).get_columns("workshop_preparations")
    }
    assert "restart_grace_until" not in before

    migrate(url)
    after = {
        item["name"] for item in inspect(engine_for(url)).get_columns("workshop_preparations")
    }
    assert {
        "cleanup_reason",
        "last_client_disconnected_at",
        "restart_grace_until",
        "workshop_complete_at",
        "cleanup_available_at",
        "cleanup_last_error_code",
    }.issubset(after)
    with engine_for(url).connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"


def test_0008_adds_append_only_evidence_assessment_store(tmp_path):
    url = f"sqlite:///{tmp_path / 'evidence-assessments.sqlite'}"
    migrate(url)
    command.downgrade(_alembic(url), "0007")
    assert not set(EVIDENCE_ASSESSMENT_TABLE_NAMES).intersection(
        inspect(engine_for(url)).get_table_names()
    )

    migrate(url)
    inspector = inspect(engine_for(url))
    assert set(EVIDENCE_ASSESSMENT_TABLE_NAMES).issubset(inspector.get_table_names())
    with engine_for(url).connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE "
                    "'workshop_artifact_evidence_assessment%_no_%'"
                )
            )
        }
    assert triggers == {
        f"{table_name}_{operation}"
        for table_name in EVIDENCE_ASSESSMENT_TABLE_NAMES
        for operation in ("no_update", "no_delete")
    }


def test_semantic_adapters_never_guess_confirmation_or_identity_bindings():
    assert require_unique_historical_binding(("CONF-1",), binding_kind="confirmation") == "CONF-1"
    for candidates in ((), ("CONF-1", "CONF-2")):
        with pytest.raises(MigrationRebindingRequired):
            require_unique_historical_binding(candidates, binding_kind="confirmation")

    later = migrate_legacy_readiness("LATER_REVIEW")
    assert (later.readiness, later.review_obligation) == ("READY", "LATER_REVIEW")
    decision = migrate_legacy_readiness("READY", human_decision_missing=True)
    assert (decision.readiness, decision.review_obligation) == (
        "NEEDS_CLARIFICATION", "DECISION_REQUIRED"
    )
    with pytest.raises(MigrationRebindingRequired):
        migrate_legacy_readiness(
            "READY",
            still_being_synthesized=True,
            human_decision_missing=True,
        )
