from fastapi import FastAPI
from sqlalchemy import text

from app.common.database import Base, engine


app = FastAPI(
    title="TC Backend API",
    description="MemoryKeeper & AstroJournal Common Backend",
    version="1.0.0"
)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


@app.get("/")
def root():
    return {
        "service": "TC Backend",
        "status": "running"
    }


@app.get("/health")
def health_check():
    return {
        "status": "ok"
    }


@app.get("/db-test")
def db_test():
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            value = result.scalar()

        return {
            "database": "connected",
            "result": value
        }

    except Exception as e:
        return {
            "database": "failed",
            "error": str(e)
        }