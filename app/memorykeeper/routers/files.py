from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.file import (
    MemoryKeeperBatchAssignPlaceRequest,
    MemoryKeeperBatchAssignPlaceResponse,
    MemoryKeeperFileDeleteResponse,
    MemoryKeeperFileMetadataResponse,
    MemoryKeeperFileMetadataUpdate,
    MemoryKeeperPlaceStateQueryRequest,
    MemoryKeeperPlaceStateQueryResponse,
)
from app.memorykeeper.services.file_place_batch_service import (
    MemoryKeeperFilePlaceBatchService,
)
from app.memorykeeper.services.file_service import MemoryKeeperFileService


router = APIRouter(prefix="/api/memorykeeper/files", tags=["MemoryKeeper Files"])


@router.post("/place-state/query", response_model=MemoryKeeperPlaceStateQueryResponse)
def query_file_place_states(
    payload: MemoryKeeperPlaceStateQueryRequest,
    db: Session = Depends(get_db),
) -> MemoryKeeperPlaceStateQueryResponse:
    return MemoryKeeperFilePlaceBatchService(db).query_states(payload)


@router.post("/assign-place", response_model=MemoryKeeperBatchAssignPlaceResponse)
def assign_files_to_place(
    payload: MemoryKeeperBatchAssignPlaceRequest,
    db: Session = Depends(get_db),
) -> MemoryKeeperBatchAssignPlaceResponse:
    return MemoryKeeperFilePlaceBatchService(db).assign_place(payload)


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
