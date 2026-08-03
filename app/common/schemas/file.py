from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FileCreate(BaseModel):
    """공통 파일 생성 요청 스키마."""

    file_id: str
    original_name: str
    extension: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    original_path: str | None = None
    preview_path: str | None = None
    thumb_path: str | None = None


class FileResponse(BaseModel):
    """공통 파일 응답 스키마."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: str
    original_name: str
    extension: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    original_path: str | None = None
    preview_path: str | None = None
    thumb_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted: bool = False
