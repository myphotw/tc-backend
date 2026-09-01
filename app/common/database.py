from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.config import settings
# Compatibility re-export for existing ``from app.common.database import Base`` callers.
from app.common.model_registry import Base


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
