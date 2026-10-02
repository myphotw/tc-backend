from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.responses import FileResponse, StreamingResponse

from app.common.routers.gallery import _build_original_response
from app.common.services.http_range import (
    ByteRange,
    ByteRangeNotSatisfiable,
    iter_file_range,
    parse_single_byte_range,
)


def _fixture(path: Path, *, size: int = 64) -> bytes:
    content = bytes(range(size))
    path.write_bytes(content)
    return content


async def _streaming_body(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


async def _file_response_body(response: FileResponse) -> bytes:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await response(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/original",
            "raw_path": b"/original",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("test-client", 50000),
            "server": ("test-server", 80),
        },
        receive,
        send,
    )
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


def test_full_get_streams_identical_bytes_with_length_range_and_mime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "photo.jpg"
    content = _fixture(path)

    response = _build_original_response(
        path=path,
        media_type="image/jpeg",
        range_header=None,
        head_only=False,
    )

    assert isinstance(response, FileResponse)
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(content))
    assert response.headers["content-type"] == "image/jpeg"
    assert asyncio.run(_file_response_body(response)) == content


def test_closed_range_returns_exact_requested_bytes(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    content = _fixture(path)

    response = _build_original_response(
        path=path,
        media_type="video/mp4",
        range_header="bytes=10-19",
        head_only=False,
    )

    assert isinstance(response, StreamingResponse)
    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 10-19/64"
    assert response.headers["content-length"] == "10"
    assert response.headers["content-type"] == "video/mp4"
    assert asyncio.run(_streaming_body(response)) == content[10:20]


@pytest.mark.parametrize(
    ("range_header", "expected_range", "expected_bytes"),
    [
        ("bytes=10-", ByteRange(10, 63), bytes(range(10, 64))),
        ("bytes=-10", ByteRange(54, 63), bytes(range(54, 64))),
        ("bytes=10-999999", ByteRange(10, 63), bytes(range(10, 64))),
    ],
)
def test_open_suffix_and_overflow_ranges(
    tmp_path: Path,
    range_header: str,
    expected_range: ByteRange,
    expected_bytes: bytes,
) -> None:
    path = tmp_path / "clip.mp4"
    _fixture(path)

    parsed = parse_single_byte_range(range_header, file_size=64)
    response = _build_original_response(
        path=path,
        media_type="video/mp4",
        range_header=range_header,
        head_only=False,
    )

    assert parsed == expected_range
    assert isinstance(response, StreamingResponse)
    assert response.headers["content-range"] == (
        f"bytes {expected_range.start}-{expected_range.end}/64"
    )
    assert asyncio.run(_streaming_body(response)) == expected_bytes


@pytest.mark.parametrize(
    "range_header",
    [
        "bytes=64-",
        "bytes=99-100",
        "bytes=20-10",
        "bytes=-0",
        "bytes=-",
        "items=0-1",
        "bytes=bad",
    ],
)
def test_malformed_and_unsatisfiable_ranges_return_416(
    tmp_path: Path,
    range_header: str,
) -> None:
    path = tmp_path / "clip.mp4"
    _fixture(path)

    with pytest.raises(ByteRangeNotSatisfiable):
        parse_single_byte_range(range_header, file_size=64)
    response = _build_original_response(
        path=path,
        media_type="video/mp4",
        range_header=range_header,
        head_only=False,
    )

    assert response.status_code == 416
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes */64"


def test_multiple_ranges_are_rejected_with_416(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    _fixture(path)

    response = _build_original_response(
        path=path,
        media_type="video/mp4",
        range_header="bytes=0-9,20-29",
        head_only=False,
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */64"


def test_pathologically_large_numeric_range_is_rejected_without_500(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clip.mp4"
    _fixture(path)
    range_header = "bytes=" + ("9" * 5000) + "-"

    response = _build_original_response(
        path=path,
        media_type="video/mp4",
        range_header=range_header,
        head_only=False,
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */64"


def test_head_returns_metadata_without_body(tmp_path: Path) -> None:
    path = tmp_path / "clip.mov"
    _fixture(path)

    response = _build_original_response(
        path=path,
        media_type="video/quicktime",
        range_header=None,
        head_only=True,
    )

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "64"
    assert response.headers["content-type"] == "video/quicktime"
    assert response.body == b""


def test_file_iterator_never_reads_more_than_requested_or_chunk_size(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.bin"
    content = _fixture(path)

    chunks = list(iter_file_range(path, start=5, length=31, chunk_size=7))

    assert all(len(chunk) <= 7 for chunk in chunks)
    assert b"".join(chunks) == content[5:36]
