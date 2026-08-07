from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.common.config import settings


DATABASE_URL = (
    f"postgresql://"
    f"{settings.POSTGRES_USER}:"
    f"{quote_plus(settings.POSTGRES_PASSWORD)}@"
    f"{settings.POSTGRES_HOST}:"
    f"{settings.POSTGRES_PORT}/"
    f"{settings.POSTGRES_DB}"
)


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=15,
    max_overflow=30,
    pool_timeout=30,
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


Base = declarative_base()


# 모델 등록
from app.common.models import api_key
from app.common.models import setting
from app.common.models import file
from app.common.models import file_service
from app.common.models import upload_job
from app.common.models import file_metadata
from app.common.models import metadata_history
from app.common.models import file_tag
from app.common.models import vision_job
from app.common.models import api_usage
from app.common.models import geocode_cache
from app.common.models import worker_status

from app.memorykeeper.models import photo
from app.memorykeeper.models import tag
from app.memorykeeper.models import photo_tag


def get_db():
    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


def initialize_database():
    """테이블 생성 + 누락 컬럼/인덱스 동기화 (기존 데이터 유지)."""
    from app.common.schema_sync import initialize_database as _initialize_database

    return _initialize_database(engine)
