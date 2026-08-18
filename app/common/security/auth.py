from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="TCBackendBearer",
    description="TC Backend access token",
)


def require_backend_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    """Protect API routes when ``TC_BACKEND_AUTH_TOKEN`` is configured."""
    expected_token = settings.TC_BACKEND_AUTH_TOKEN
    if expected_token is None or not expected_token.strip():
        return

    raw_header = request.headers.get("authorization")
    if raw_header is None:
        _raise_unauthorized(request, reason="missing_header")
    if credentials is None:
        _raise_unauthorized(request, reason="malformed_header")
    if credentials.scheme.casefold() != "bearer":
        _raise_unauthorized(request, reason="invalid_scheme")

    supplied_token = credentials.credentials
    if not supplied_token:
        _raise_unauthorized(request, reason="empty_token")
    if not secrets.compare_digest(
        supplied_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    ):
        _raise_unauthorized(request, reason="invalid_token")


def _raise_unauthorized(request: Request, *, reason: str) -> None:
    client_ip = request.client.host if request.client is not None else None
    logger.warning(
        "backend_auth_failed method=%s path=%s client_ip=%s reason=%s",
        request.method,
        request.url.path,
        client_ip,
        reason,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "UNAUTHORIZED",
            "message": "Authentication required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )
