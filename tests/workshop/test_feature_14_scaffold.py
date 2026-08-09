from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specops_workflow import FrozenClock
from specops_workshop.api import create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.delegation import (
    DEV_LEAD_ACTOR_ID,
    EXPECTED_ATTACHMENT_HASH,
    PM_ACTOR_ID,
    REQUIRED_COMMAND_SCOPE,
    load_delegation_fixture,
)
from specops_workshop.sources import SourceCatalog, SourceName


SPEC_ENG_ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)


def settings(tmp_path: Path, **updates: str) -> Settings:
    values = {
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-test-value",
        "OPENAI_API_KEY": "openai-test-value",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    }
    values.update(updates)
    return Settings.load(values)


def test_settings_are_closed_server_only_and_reject_platform_configuration(tmp_path):
    configured = settings(tmp_path)
    assert configured.gemini_model == GEMINI_MODEL
    assert configured.terra_model == TERRA_MODEL
    assert configured.analyzer_reasoning_effort == "medium"
    assert settings(
        tmp_path, OPENAI_ANALYZER_REASONING_EFFORT="high"
    ).analyzer_reasoning_effort == "high"
    with pytest.raises(ValueError):
        settings(tmp_path, OPENAI_ANALYZER_REASONING_EFFORT="flexible")
    assert "api_key" not in str(configured.model_dump(exclude={"gemini_api_key", "openai_api_key"}))
    with pytest.raises(ValueError, match="outside the Workshop boundary"):
        settings(tmp_path, GITHUB_TOKEN="forbidden")
    with pytest.raises(ValueError, match="pinned release models"):
        settings(tmp_path, GEMINI_LIVE_MODEL="another-model")
    with pytest.raises(ValueError, match="missing required"):
        Settings.load({})


def test_fixture_and_source_catalog_are_exact_and_never_read_excluded_contract(monkeypatch):
    reads: list[Path] = []
    original = Path.read_bytes

    def observed(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", observed)
    catalog = SourceCatalog(SPEC_ENG_ROOT)
    fixture = load_delegation_fixture(catalog, now=NOW)
    assert fixture.delegator_actor_id == DEV_LEAD_ACTOR_ID
    assert fixture.delegate_actor_id == PM_ACTOR_ID
    assert fixture.command_scope == REQUIRED_COMMAND_SCOPE
    assert fixture.attachment_hash == EXPECTED_ATTACHMENT_HASH
    assert fixture.valid_from <= NOW <= fixture.valid_until
    assert catalog.digest(SourceName.TECHNICAL_SPEC) == EXPECTED_ATTACHMENT_HASH
    excluded = SPEC_ENG_ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-contract.md"
    assert excluded not in reads
    assert set(reads).issubset({
        catalog.document(SourceName.DELEGATION_FIXTURE).path,
        catalog.document(SourceName.TECHNICAL_SPEC).path,
    })


def test_fixture_hash_and_expiry_fail_closed(tmp_path):
    root = tmp_path / "spec-eng"
    (root / "docs/technical-specs").mkdir(parents=True)
    (root / "fixtures").mkdir()
    technical = SPEC_ENG_ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md"
    fixture = SPEC_ENG_ROOT / "fixtures/dev-lead-delegation-email.md"
    (root / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md").write_bytes(technical.read_bytes())
    text = fixture.read_text(encoding="utf-8").replace(str(technical), str(root / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md"))
    (root / "fixtures/dev-lead-delegation-email.md").write_text(text, encoding="utf-8")
    catalog = SourceCatalog(root)
    with pytest.raises(ValueError, match="not active"):
        load_delegation_fixture(catalog, now=datetime(2026, 8, 23, tzinfo=timezone.utc))
    changed = text.replace(EXPECTED_ATTACHMENT_HASH, "0" * 64)
    (root / "fixtures/dev-lead-delegation-email.md").write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="attachment mismatch"):
        load_delegation_fixture(catalog, now=NOW)


def test_app_bootstrap_is_idempotent_contains_no_secrets_and_has_no_login(tmp_path):
    configured = settings(tmp_path)
    clock = FrozenClock(NOW)
    first = create_app(settings=configured, clock=clock, source_catalog=SourceCatalog(SPEC_ENG_ROOT))
    response = TestClient(first).get("/api/bootstrap")
    assert response.status_code == 200
    payload = response.json()
    serialized = response.text.lower()
    assert payload["pm_actor_id"] == str(PM_ACTOR_ID)
    assert payload["dev_lead_actor_id"] == str(DEV_LEAD_ACTOR_ID)
    assert payload["technical_source_lines"]
    assert all(value not in serialized for value in ("gemini-test-value", "openai-test-value", "jira_", "github_"))
    assert all("login" not in route.path and "auth" not in route.path for route in first.routes)
    second = create_app(settings=configured, clock=clock, source_catalog=SourceCatalog(SPEC_ENG_ROOT))
    assert TestClient(second).get("/api/bootstrap").json() == payload
