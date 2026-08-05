from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.db import Base, engine
from app.security import require_admin


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    Base.metadata.create_all(bind=engine)
    yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
def protected_openapi():
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_admin)])
def protected_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{settings.app_name} API")
