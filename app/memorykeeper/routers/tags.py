from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.file import (
    FileTagMutationRequest,
    FileTagMutationResponse,
)
from app.memorykeeper.schemas.tag import (
    TagCreate,
    TagListResponse,
    TagMergeRequest,
    TagResponse,
    TagUpdate,
)
from app.memorykeeper.services.tag_service import MemoryKeeperTagService


router = APIRouter(prefix="/api/memorykeeper", tags=["MemoryKeeper Tags"])


@router.get("/tags", response_model=TagListResponse)
def list_tags(
    query: str | None = Query(None, max_length=100),
    favorite: bool | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TagListResponse:
    return MemoryKeeperTagService(db).list(
        query=query,
        favorite=favorite,
        limit=limit,
        offset=offset,
    )


@router.post("/tags", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(payload: TagCreate, db: Session = Depends(get_db)) -> TagResponse:
    return MemoryKeeperTagService(db).create(payload)


@router.patch("/tags/{tag_id}", response_model=TagResponse)
def update_tag(tag_id: int, payload: TagUpdate, db: Session = Depends(get_db)) -> TagResponse:
    return MemoryKeeperTagService(db).update(tag_id, payload)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int,
    expected_revision: int = Query(..., ge=1),
    db: Session = Depends(get_db),
) -> Response:
    MemoryKeeperTagService(db).delete(tag_id, expected_revision=expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tags/{tag_id}/merge", response_model=TagResponse)
def merge_tag(
    tag_id: int,
    payload: TagMergeRequest,
    db: Session = Depends(get_db),
) -> TagResponse:
    return MemoryKeeperTagService(db).merge(tag_id, payload)


@router.post("/files/{file_id}/tags/{tag_id}", response_model=FileTagMutationResponse)
def assign_file_tag(
    file_id: str,
    tag_id: int,
    payload: FileTagMutationRequest,
    db: Session = Depends(get_db),
) -> FileTagMutationResponse:
    return MemoryKeeperTagService(db).assign(
        file_id,
        tag_id,
        expected_revision=payload.expected_revision,
    )


@router.delete("/files/{file_id}/tags/{tag_id}", response_model=FileTagMutationResponse)
def remove_file_tag(
    file_id: str,
    tag_id: int,
    expected_revision: int = Query(..., ge=0),
    db: Session = Depends(get_db),
) -> FileTagMutationResponse:
    return MemoryKeeperTagService(db).remove(
        file_id,
        tag_id,
        expected_revision=expected_revision,
    )
