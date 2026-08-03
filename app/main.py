from app.common.routers import api_keys, monitoring, upload
from fastapi import FastAPI
from sqlalchemy import text

from app.common.database import Base, engine


app = FastAPI(
    title="TC Backend API",
    description="MemoryKeeper & AstroJournal Common Backend",
    version="1.0.0"
)

app.include_router(api_keys.router)
app.include_router(upload.router)
app.include_router(monitoring.router)


@app.on_event("startup")
def startup():
    try:
        Base.metadata.create_all(bind=engine)
        print("Database initialization completed")
    except Exception as e:
        print("Database initialization skipped")
        print(e)

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