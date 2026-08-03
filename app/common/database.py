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
    pool_pre_ping=True
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