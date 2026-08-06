# TC-Backend Architecture (v1.0.0 Freeze)

MemoryKeeper / AstroJournal 공통 Backend.

## 개요

- FastAPI HTTP API
- PostgreSQL + SQLAlchemy ORM
- UploadWorker / VisionWorker Plugin Pipeline
- Storage Rule Engine (MemoryKeeper)
- PC Folder Watcher (선택)

## 레이어

```
Router → Service → Repository → Model(DB)
                ↘ StorageService / API Clients
Worker → PluginManager → Plugins → Repository / Clients
```

## 디렉터리

```
app/
  common/
    config.py
    database.py
    models/
    repositories/
    routers/
    schemas/
    services/
    security/
  main.py
worker/
  background_worker.py
  vision_worker.py
  worker_monitor.py
  plugins/
watcher/
docs/
```

## 버전

- `settings.VERSION = "1.0.0"`
- Health / Dashboard / WorkerStatus / FastAPI OpenAPI에 노출

## 관련 문서

- [API_REFERENCE.md](./API_REFERENCE.md)
- [DATABASE_ERD.md](./DATABASE_ERD.md)
- [PLUGIN_GUIDE.md](./PLUGIN_GUIDE.md)
- [WORKER_GUIDE.md](./WORKER_GUIDE.md)
