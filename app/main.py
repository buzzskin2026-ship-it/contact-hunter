from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.db import init_db
from app.security import require_admin
from app.services.job_recovery import resume_pending_jobs

APP_RELEASE = "2026.08.06-discovery-v6"
VALIDATION = "pending-ci"
VALIDATION_RUN = "pending"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    init_db()
    resume_pending_jobs()
    yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.6.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)


@app.get("/version", include_in_schema=False)
def deployed_version() -> dict[str, str]:
    return {
        "release": APP_RELEASE,
        "validation": VALIDATION,
        "validation_run": VALIDATION_RUN,
        "commit": os.getenv("RENDER_GIT_COMMIT", "local"),
        "branch": os.getenv("RENDER_GIT_BRANCH", "local"),
        "repository": os.getenv("RENDER_GIT_REPO_SLUG", "local"),
    }


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
def protected_openapi():
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_admin)])
def protected_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{settings.app_name} API")
