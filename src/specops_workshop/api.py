from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from specops_workflow import SystemClock

from .bootstrap import BootstrapView, bootstrap_foundation
from .config import Settings
from .delegation import load_delegation_fixture
from .sources import SourceCatalog


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SPEC_ENG_ROOT = BACKEND_ROOT.parent / "Spec_Eng"


def create_app(
    *,
    settings: Settings | None = None,
    clock=None,
    source_catalog: SourceCatalog | None = None,
) -> FastAPI:
    runtime_clock = clock or SystemClock()
    runtime_settings = settings or Settings.load(
        os.environ,
        Path(os.environ.get("SPECOPS_ENV_FILE", SPEC_ENG_ROOT / ".env")),
    )
    catalog = source_catalog or SourceCatalog(SPEC_ENG_ROOT)
    fixture = load_delegation_fixture(catalog, now=runtime_clock.now())
    workflow, bootstrap = bootstrap_foundation(
        runtime_settings,
        catalog,
        fixture,
        clock=runtime_clock,
    )
    app = FastAPI(title="SpecOps Workshop", docs_url=None, redoc_url=None)
    app.state.settings = runtime_settings
    app.state.workflow = workflow
    app.state.bootstrap = bootstrap
    app.state.source_catalog = catalog

    @app.get("/api/bootstrap", response_model=BootstrapView)
    async def bootstrap_view() -> BootstrapView:
        return app.state.bootstrap

    frontend_dist = BACKEND_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            candidate = frontend_dist / path
            if path and candidate.is_file() and candidate.resolve().is_relative_to(frontend_dist.resolve()):
                return FileResponse(candidate)
            index = frontend_dist / "index.html"
            if index.is_file():
                return FileResponse(index)
            raise HTTPException(status_code=404)
    return app
