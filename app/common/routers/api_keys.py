from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.models.api_key import ApiKey
from app.common.schemas import ApiKeyCreate
from app.common.security.crypto import encrypt_value


router = APIRouter(
    prefix="/api/common/api-keys",
    tags=["API Keys"]
)


@router.get("/")
def get_api_keys(
    db: Session = Depends(get_db)
):
    return db.query(ApiKey).all()


@router.post("/")
def create_api_key(
    data: ApiKeyCreate,
    db: Session = Depends(get_db)
):
    item = ApiKey(
    service_name=data.service_name,
    api_key=encrypt_value(data.api_key),
    description=data.description
)

    db.add(item)
    db.commit()
    db.refresh(item)

    return item


@router.delete("/{key_id}")
def delete_api_key(
    key_id: int,
    db: Session = Depends(get_db)
):
    item = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id)
        .first()
    )

    if item:
        db.delete(item)
        db.commit()

    return {
        "status": "deleted"
    }