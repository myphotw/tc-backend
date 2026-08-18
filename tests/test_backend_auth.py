from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from app.common.config import settings
from app.main import app
from watcher.upload_client import UploadClient


@dataclass
class AsgiResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)


def request_app(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> AsgiResponse:
    """Exercise the real ASGI app without Starlette's optional httpx2 client."""
    parsed = urlsplit(url)
    messages: list[dict[str, object]] = []
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "client": ("test-client", 50000),
        "server": ("test-server", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start["headers"]
    }
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return AsgiResponse(
        status_code=int(start["status"]),
        headers=response_headers,
        content=content,
    )


class BackendAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_token = settings.TC_BACKEND_AUTH_TOKEN
        settings.TC_BACKEND_AUTH_TOKEN = "test-only-backend-token"

    def tearDown(self) -> None:
        settings.TC_BACKEND_AUTH_TOKEN = self.original_token

    def test_missing_wrong_basic_and_empty_credentials_return_standard_401(self) -> None:
        cases = (
            {},
            {"Authorization": "Bearer wrong-test-token"},
            {"Authorization": "Basic dGVzdDp0ZXN0"},
            {"Authorization": "Bearer "},
            {"Authorization": "test-only-backend-token"},
        )
        for headers in cases:
            with self.subTest(headers=bool(headers)):
                response = request_app(
                    "get",
                    "/api/common/capabilities",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json(),
                    {
                        "detail": {
                            "code": "UNAUTHORIZED",
                            "message": "Authentication required",
                        }
                    },
                )
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_valid_bearer_uses_constant_time_comparison_and_keeps_response(self) -> None:
        with patch(
            "app.common.security.auth.secrets.compare_digest",
            wraps=__import__("secrets").compare_digest,
        ) as compare_digest:
            response = request_app(
                "get",
                "/api/common/capabilities",
                headers={"Authorization": "Bearer test-only-backend-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["capabilities"]["upload"])
        compare_digest.assert_called_once_with(
            b"test-only-backend-token",
            b"test-only-backend-token",
        )
        self.assertNotIn("auth", str(response.json()).lower())

    def test_public_health_and_db_test_do_not_require_auth(self) -> None:
        health = request_app("get", "/health")
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.scalar.return_value = 1
        with patch("app.main.engine.connect", return_value=connection):
            database = request_app("get", "/db-test")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(database.status_code, 200)
        self.assertEqual(database.json()["database"], "connected")

    def test_db_test_failure_does_not_expose_connection_details(self) -> None:
        sensitive_error = "postgresql://user:password@private-host:5432/database"
        with patch("app.main.engine.connect", side_effect=RuntimeError(sensitive_error)):
            response = request_app("get", "/db-test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], "connection_failed")
        self.assertNotIn(sensitive_error, response.text)

    def test_sensitive_and_cost_bearing_routes_are_protected(self) -> None:
        routes = (
            ("get", "/api/common/api-keys/"),
            ("get", "/"),
            ("post", "/api/common/upload"),
            ("get", "/api/common/upload/jobs"),
            ("get", "/api/common/gallery"),
            ("get", "/api/common/changes"),
            ("get", "/api/common/health"),
            ("get", "/api/common/readiness"),
            ("get", "/api/astro/records"),
            ("get", "/api/astro/gallery"),
            ("get", "/api/common/geocoding/reverse"),
            ("get", "/api/common/places/autocomplete"),
            ("get", "/api/common/weather/current"),
            ("post", "/api/astro/plate-solve"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                response = request_app(method, path)
                self.assertEqual(response.status_code, 401)

    def test_every_registered_api_operation_declares_bearer_security(self) -> None:
        schema = app.openapi()
        self.assertIn(
            {"TCBackendBearer": []},
            schema["paths"]["/"]["get"].get("security", []),
        )
        protected_operations = 0
        for path, path_item in schema["paths"].items():
            if not path.startswith("/api/"):
                continue
            for method in ("get", "post", "put", "patch", "delete"):
                operation = path_item.get(method)
                if operation is None:
                    continue
                protected_operations += 1
                self.assertIn(
                    {"TCBackendBearer": []},
                    operation.get("security", []),
                    msg=f"Missing bearer security: {method.upper()} {path}",
                )
        self.assertGreater(protected_operations, 0)

    def test_unconfigured_or_blank_token_keeps_lan_compatibility(self) -> None:
        for token in (None, "   "):
            with self.subTest(token=token):
                settings.TC_BACKEND_AUTH_TOKEN = token
                response = request_app("get", "/api/common/capabilities")
                self.assertEqual(response.status_code, 200)

    def test_folder_watcher_upload_client_adds_bearer_header(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"job_id": "test-job", "status": "WAITING"}
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "watch.jpg"
            file_path.write_bytes(b"watcher-test")
            with patch.dict(
                os.environ,
                {"TC_BACKEND_AUTH_TOKEN": "test-only-watcher-token"},
            ):
                client = UploadClient(retry_count=1)
                with patch(
                    "watcher.upload_client.requests.post",
                    return_value=response,
                ) as post:
                    result = client.upload(file_path)

        self.assertEqual(result["job_id"], "test-job")
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"Authorization": "Bearer test-only-watcher-token"},
        )


if __name__ == "__main__":
    unittest.main()
