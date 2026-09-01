from app.common.routers import (
    api_keys,
    capabilities,
    changes,
    external_apis,
    gallery,
    monitoring,
    upload,
    upload_jobs,
)
from app.astrojournal.routers import gallery as astro_gallery
from app.astrojournal.routers import events as astrojournal_events
from app.astrojournal.routers import observation_records
from app.astrojournal.routers import plate_solve
from app.astrojournal.routers import reset as astrojournal_reset
from app.memorykeeper.routers import places as memorykeeper_places
from app.memorykeeper.routers import gallery as memorykeeper_gallery
from app.memorykeeper.routers import files as memorykeeper_files
from app.memorykeeper.routers import pending as memorykeeper_pending
from app.memorykeeper.routers import tags as memorykeeper_tags
from app.memorykeeper.routers import auto_tags as memorykeeper_auto_tags
from app.memorykeeper.routers import reset as memorykeeper_reset
import logging
import time

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.common.config import settings
from app.common.database import engine, initialize_database
from app.common.security import require_backend_auth
from app.common.utils.perf import elapsed_ms, log_perf, new_request_id

logger = logging.getLogger(__name__)


app = FastAPI(
    title="TC Backend API",
    description=(
        "MemoryKeeper & AstroJournal Common Backend (Version Freeze 1.0). "
        "Upload, Gallery, Monitoring, Health APIs."
    ),
    version=settings.VERSION,
)


_SKIP_PERF_PATH_PREFIXES = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/health",
)


class TimingMiddleware(BaseHTTPMiddleware):
    """모든 HTTP 요청 소요시간 계측."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        request.state.request_id = request_id
        started = time.perf_counter()
        response: Response | None = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            ms = elapsed_ms(started)
            path = request.url.path
            should_log = not any(path.startswith(prefix) for prefix in _SKIP_PERF_PATH_PREFIXES)
            response_size = None
            if response is not None:
                response.headers["X-Process-Time-Ms"] = str(ms)
                response.headers["X-Request-Id"] = request_id
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    response_size = int(content_length)
            if should_log:
                log_perf(
                    "http_request",
                    request_id=request_id,
                    method=request.method,
                    path=path,
                    status_code=status_code,
                    elapsed_ms=ms,
                    response_size=response_size,
                )


app.add_middleware(TimingMiddleware)

protected_api_router = APIRouter(
    dependencies=[Depends(require_backend_auth)],
)
protected_api_router.include_router(api_keys.router)
protected_api_router.include_router(upload.router)
protected_api_router.include_router(upload_jobs.router)
protected_api_router.include_router(capabilities.router)
protected_api_router.include_router(changes.router)
protected_api_router.include_router(external_apis.router)
protected_api_router.include_router(monitoring.router)
protected_api_router.include_router(gallery.router)
protected_api_router.include_router(observation_records.router)
protected_api_router.include_router(astro_gallery.router)
protected_api_router.include_router(astrojournal_events.router)
protected_api_router.include_router(plate_solve.router)
protected_api_router.include_router(astrojournal_reset.router)
protected_api_router.include_router(memorykeeper_places.router)
protected_api_router.include_router(memorykeeper_gallery.router)
protected_api_router.include_router(memorykeeper_files.router)
protected_api_router.include_router(memorykeeper_tags.router)
protected_api_router.include_router(memorykeeper_auto_tags.router)
protected_api_router.include_router(memorykeeper_reset.router)
protected_api_router.include_router(memorykeeper_pending.router)
app.include_router(protected_api_router)


@app.on_event("startup")
def startup():
    if settings.TC_BACKEND_AUTH_TOKEN is None:
        logger.warning("TC_BACKEND_AUTH_TOKEN is not configured")
    try:
        changes = initialize_database()
        print(f"Database initialization completed (version={settings.VERSION})")
        if changes:
            print(f"Schema sync applied: {changes}")
    except Exception as e:
        print("Database initialization skipped")
        print(e)


@app.get(
    "/",
    tags=["System"],
    dependencies=[Depends(require_backend_auth)],
)
def root():
    """서비스 기본 정보."""
    return {
        "service": "TC Backend",
        "status": "running",
        "version": settings.VERSION,
    }


@app.get("/health", tags=["System"])
def health_check():
    """간단 Health Check (상세는 /api/common/health)."""
    return {
        "status": "ok",
        "version": settings.VERSION,
    }


@app.get("/db-test", tags=["System"])
def db_test():
    """DB 연결 점검용 엔드포인트."""
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            value = result.scalar()

        return {
            "database": "connected",
            "result": value,
            "version": settings.VERSION,
        }

    except Exception:
        return {
            "database": "failed",
            "error": "connection_failed",
            "version": settings.VERSION,
        }
