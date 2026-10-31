import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import get_settings
from app.db import get_engine, ping
from app.log_config import configure_logging
from app.routers import bookings, trains

configure_logging(get_settings().log_level)
log = structlog.get_logger("railbook.http")

# Set by Kong's correlation-id plugin; generated here when calling the API directly.
REQUEST_ID_HEADER = "X-Request-ID"
QUIET_PATHS = {"/metrics", "/healthz", "/readyz"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("startup")
    yield
    # Twelve-Factor IX: uvicorn has drained in-flight requests; release the DB pool.
    get_engine().dispose()
    log.info("shutdown_complete")


app = FastAPI(title="RailBook", version="0.1.0", lifespan=lifespan)
app.include_router(trains.router)
app.include_router(bookings.router)

# The frontend: static files shipped in the same image, served from the same origin as the API
# (so the browser needs no CORS rules).
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Static assets would only add noise to the request metrics.
Instrumentator(excluded_handlers=[*QUIET_PATHS, "/static"]).instrument(app).expose(
    app, include_in_schema=False
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.middleware("http")
async def request_context(request: Request, call_next) -> Response:
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        if request.url.path not in QUIET_PATHS:
            route = request.scope.get("route")
            log.info(
                "request",
                method=request.method,
                path=getattr(route, "path", request.url.path),
                status=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )


@app.get("/auth-config", include_in_schema=False)
def auth_config() -> dict:
    """Where the frontend logs in (factor III: config from the environment, not from app.js).
    Public values only."""
    settings = get_settings()
    return {"issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(response: Response) -> dict:
    if not ping():
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}
