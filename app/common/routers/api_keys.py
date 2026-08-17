from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.models.api_key import ApiKey
from app.common.schemas import ApiKeyCreate, ApiKeyStatus, ApiKeyUpdate
from app.common.security.crypto import encrypt_value


router = APIRouter(
    prefix="/api/common/api-keys",
    tags=["API Keys"],
)


@router.get(
    "/",
    summary="List API keys",
    description="List key configuration metadata without returning key material.",
    response_model=list[ApiKeyStatus],
)
def get_api_keys(db: Session = Depends(get_db)) -> list[ApiKeyStatus]:
    return [_to_status(item) for item in db.query(ApiKey).all()]


@router.post(
    "/",
    summary="Create API key",
    description="Encrypt and store a server-side external API key.",
    status_code=200,
    response_model=ApiKeyStatus,
)
def create_api_key(
    data: ApiKeyCreate,
    db: Session = Depends(get_db),
) -> ApiKeyStatus:
    item = ApiKey(
        service_name=data.service_name.value,
        api_key=encrypt_value(data.api_key),
        description=data.description,
        enabled=data.enabled,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "API_KEY_ALREADY_EXISTS"},
        ) from exc
    db.refresh(item)
    return _to_status(item)


@router.patch(
    "/{key_id}",
    summary="Update API key",
    response_model=ApiKeyStatus,
)
def update_api_key(
    key_id: int,
    data: ApiKeyUpdate,
    db: Session = Depends(get_db),
) -> ApiKeyStatus:
    item = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "API_KEY_NOT_FOUND"})

    if "api_key" in data.model_fields_set:
        if not data.api_key:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_REQUEST", "message": "api_key is required"},
            )
        item.api_key = encrypt_value(data.api_key)
    if "description" in data.model_fields_set:
        item.description = data.description
    if "enabled" in data.model_fields_set:
        if data.enabled is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_REQUEST", "message": "enabled is required"},
            )
        item.enabled = data.enabled
    db.commit()
    db.refresh(item)
    return _to_status(item)


@router.delete(
    "/{key_id}",
    summary="Delete API key",
    description="Delete a stored external API key.",
)
def delete_api_key(
    key_id: int,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    item = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if item:
        db.delete(item)
        db.commit()
    return {"status": "deleted"}


def _to_status(item: ApiKey) -> ApiKeyStatus:
    return ApiKeyStatus(
        id=item.id,
        service_name=item.service_name,
        configured=bool(item.api_key),
        enabled=bool(item.enabled),
        description=item.description,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
