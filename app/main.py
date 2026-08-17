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
from app.astrojournal.routers import observation_records
from app.astrojournal.routers import plate_solve
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.common.config import settings
from app.common.database import engine, initialize_database
from app.common.utils.perf import elapsed_ms, log_perf, new_request_id
import time


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

app.include_router(api_keys.router)
app.include_router(upload.router)
app.include_router(upload_jobs.router)
app.include_router(capabilities.router)
app.include_router(changes.router)
app.include_router(external_apis.router)
app.include_router(monitoring.router)
app.include_router(gallery.router)
app.include_router(observation_records.router)
app.include_router(astro_gallery.router)
app.include_router(plate_solve.router)


@app.on_event("startup")
def startup():
    try:
        changes = initialize_database()
        print(f"Database initialization completed (version={settings.VERSION})")
        if changes:
            print(f"Schema sync applied: {changes}")
    except Exception as e:
        print("Database initialization skipped")
        print(e)


@app.get("/", tags=["System"])
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

    except Exception as e:
        return {
            "database": "failed",
            "error": str(e),
            "version": settings.VERSION,
        }
