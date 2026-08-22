from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.file import (
    MemoryKeeperFileDeleteResponse,
    MemoryKeeperFileMetadataResponse,
    MemoryKeeperFileMetadataUpdate,
)
from app.memorykeeper.services.file_service import MemoryKeeperFileService


router = APIRouter(prefix="/api/memorykeeper/files", tags=["MemoryKeeper Files"])


@router.patch("/{file_id}/metadata", response_model=MemoryKeeperFileMetadataResponse)
def patch_file_metadata(
    file_id: str,
    payload: MemoryKeeperFileMetadataUpdate,
    db: Session = Depends(get_db),
) -> MemoryKeeperFileMetadataResponse:
    return MemoryKeeperFileService(db).patch_metadata(file_id, payload)


@router.delete("/{file_id}", response_model=MemoryKeeperFileDeleteResponse)
def delete_file(
    file_id: str,
    db: Session = Depends(get_db),
) -> MemoryKeeperFileDeleteResponse:
    return MemoryKeeperFileService(db).delete(file_id)
