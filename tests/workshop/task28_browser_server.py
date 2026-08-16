"""Offline real-FastAPI browser fixture for the Task 28 deterministic gate."""

from __future__ import annotations

import tempfile
from pathlib import Path

import uvicorn
from pydantic import SecretStr

from specops_workshop.api import create_app
from specops_workshop.config import Settings
from specops_workshop.sources import SourceCatalog

from test_feature_26_v4_production_seam import DeterministicAdapter


SPEC_ENG = next(
    parent for parent in Path(__file__).resolve().parents if parent.name == "Spec_Eng"
)
_temporary = tempfile.TemporaryDirectory(prefix="specops-task28-browser-")


app = create_app(
    settings=Settings(
        specops_database_url=f"sqlite:///{_temporary.name}/foundation.sqlite",
        workshop_database_url=f"sqlite:///{_temporary.name}/workshop.sqlite",
        openai_api_key=SecretStr("offline-not-called"),
    ),
    source_catalog=SourceCatalog(SPEC_ENG),
    analyzer_adapter=DeterministicAdapter(),
    live_provider=object(),
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8018, log_level="warning")
