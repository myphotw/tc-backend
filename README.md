# TC-Backend

MemoryKeeper & AstroJournal 공통 Backend (**Version 1.0.0 Freeze**).

## 문서

| Document | Path |
|----------|------|
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| API Reference | [docs/API_REFERENCE.md](docs/API_REFERENCE.md) |
| Database ERD | [docs/DATABASE_ERD.md](docs/DATABASE_ERD.md) |
| Plugin Guide | [docs/PLUGIN_GUIDE.md](docs/PLUGIN_GUIDE.md) |
| Worker Guide | [docs/WORKER_GUIDE.md](docs/WORKER_GUIDE.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

Swagger UI: `/docs`  
OpenAPI JSON: `/openapi.json`

## Upload contract (API 1.1)

`POST /api/common/upload` retains the legacy multipart `file` field. Optional
multipart fields are `service_name` (`MemoryKeeper` by default; allowed values:
`MemoryKeeper`, `AstroJournal`), `client_file_id`, and
`client_content_sha256` (64-character lowercase SHA-256).

The same `service_name + client_file_id` returns the existing UploadJob without
saving another incoming file. Different non-null hashes return `409 Conflict`.
File-only callers retain the original response shape; extended callers may
receive `service_name`, `client_file_id`, `backend_file_id`, and
`idempotent_replay`. Legacy Gallery endpoints default to MemoryKeeper when
`service_name` is omitted. Feature discovery is available at
`GET /api/common/capabilities`.

## Shared File Assets (B2)

`common_files` remains the physical FileAsset record, keyed by SHA-256. Service
ownership is represented by `common_file_services` (`UNIQUE(file_id,
service_name)`). A matching upload from another supported service reuses the
existing physical asset and adds only its service link; the UploadWorker records
`LINK_CREATED`. Gallery queries filter through this link table, while existing
MemoryKeeper API response fields remain unchanged. The legacy
`common_files.service_name` remains only for compatibility and schema backfill.

## AstroJournal Observation Records (B3)

`/api/astro/records` provides AstroJournal-only observation metadata for an
existing shared FileAsset. Each record references `common_files.id`, ensures an
`AstroJournal` domain link, supports soft delete, and uses `revision` for PATCH
optimistic locking (`409 Conflict` on stale updates). One active representative
record is allowed per `catalog_object_id`. MemoryKeeper APIs and their response
contracts are unchanged.

## AstroJournal Gallery Projection (B4-01)

Common Gallery remains the FileAsset read API. `GET /api/astro/gallery` and
`GET /api/astro/gallery/{record_id}` are the AstroJournal canonical read model,
joining an active `astro_observation_records` row to its active `common_files`
asset and required `AstroJournal` entry in `common_file_services`. The list uses
`page`/`page_size`, sorts by `captured_at DESC` with `created_at DESC` fallback,
and supports `catalog_object_id`, `favorite`, `date_from`, and `date_to` filters.

## AstroJournal Mutation Contract (B5-01)

`PATCH /api/astro/records/{record_id}` uses the required `revision` as the
expected revision. A successful partial update increments `revision`, refreshes
`updated_at`, and returns both the legacy `id` and canonical `record_id`. Stale
updates return `409` with `expected_revision` and `current_revision`.

Representative changes run in one transaction and an active partial UNIQUE
index enforces one representative per `catalog_object_id`. DELETE is soft and
idempotent: the first request increments revision and repeated requests return
the same deletion result. Optional `client_record_id` on POST provides
idempotent record creation through a partial UNIQUE service/client key.

## 프로젝트 구조

```
app/
  main.py
  common/
    config.py              # VERSION=1.0.0 + env settings
    models/                # SQLAlchemy models
    repositories/
    routers/               # upload, gallery, monitoring, api_keys
    schemas/
    services/              # storage, gallery, monitoring, api_clients
worker/
  background_worker.py     # UploadWorker
  vision_worker.py         # VisionWorker
  plugins/                 # Plugin Registry
watcher/                   # PC Folder Watcher
docs/
```

## Worker 구조

```
UploadWorker
  Hash → Preview → Storage → Metadata → Exif → Gps → Vision Queue

VisionWorker
  Vision Queue → VisionPlugin (Google Vision Labels → AI Tags)
```

자세한 내용: [docs/WORKER_GUIDE.md](docs/WORKER_GUIDE.md)

## Plugin 구조

| Plugin | Priority | Scope |
|--------|----------|-------|
| HashPlugin | 10 | upload |
| PreviewPlugin | 20 | upload |
| StoragePlugin | 30 | upload |
| MetadataPlugin | 40 | upload |
| ExifPlugin | 50 | upload |
| GpsPlugin | 60 | upload |
| VisionPlugin | 70 | vision |

자세한 내용: [docs/PLUGIN_GUIDE.md](docs/PLUGIN_GUIDE.md)

## Storage 구조

```
PHOTO_PLATFORM_ROOT/
  incoming/
  original/{year}/{country}/{city}/{place}/{file_id}.ext
  preview/
  thumb/
  export/
  cache/
  temp/
```

경로 계산: Storage Rule Engine (MemoryKeeper). 실제 move: StoragePlugin.

## API 목록

- `POST /api/common/upload`
- `GET /api/common/gallery`
- `GET /api/common/gallery/{file_id}`
- `GET /api/common/gallery/search`
- `GET /api/common/gallery/map`
- `GET /api/common/gallery/timeline`
- `GET /api/common/gallery/statistics`
- `GET /api/common/health`
- `GET /api/common/dashboard`
- `GET|POST|DELETE /api/common/api-keys/`

자세한 내용: [docs/API_REFERENCE.md](docs/API_REFERENCE.md)

## ERD

[docs/DATABASE_ERD.md](docs/DATABASE_ERD.md)

주요 테이블: `common_files`, `common_file_metadata`, `common_file_tags`, `common_upload_jobs`, `common_vision_jobs`, `common_api_usage`, `common_geocode_cache`, `common_metadata_history`, `common_worker_status`

## 환경변수

`.env` 예시 (값은 환경에 맞게 설정).

### 필수

| Key | Description |
|-----|-------------|
| `POSTGRES_HOST` | DB host |
| `POSTGRES_PORT` | DB port |
| `POSTGRES_DB` | DB name |
| `POSTGRES_USER` | DB user |
| `POSTGRES_PASSWORD` | DB password |
| `MASTER_KEY` | API key 암호화 키 |

> DB URL은 `POSTGRES_*`로 조합한다. 별도 `DATABASE_URL` 환경변수는 사용하지 않는다.

### 선택 / 기본값

| Key | Default | Description |
|-----|---------|-------------|
| `VERSION` | `1.0.0` | 플랫폼 버전 |
| `PHOTO_PLATFORM_ROOT` | `./PhotoPlatform` | 스토리지 루트 |
| `INCOMING_DIR` / `ORIGINAL_DIR` / `PREVIEW_DIR` / `THUMB_DIR` / `EXPORT_DIR` / `CACHE_DIR` / `TEMP_DIR` | root 하위 | 개별 경로 override |
| `GOOGLE_API_KEY` | none | Geocoding 등 |
| `GOOGLE_VISION_CREDENTIAL` | none | Vision Service Account JSON 경로 |
| `WEATHER_API_KEY` | none | Weather |
| `ASTROMETRY_API_KEY` | none | Platesolve |
| `API_CLIENT_TIMEOUT` | `30` | HTTP timeout(sec) |
| `API_CLIENT_RETRY_COUNT` | `3` | HTTP retry |
| `VISION_MONTHLY_LIMIT` | `1000` | Vision 월간 unit |
| `GEOCODING_MONTHLY_LIMIT` | `100000` | Geocoding 월간 unit |
| `WEATHER_MONTHLY_LIMIT` | `100000` | Weather 월간 unit |
| `PLATESOLVE_MONTHLY_LIMIT` | `100000` | Platesolve 월간 unit |

## 실행 방법

```bash
# 의존성
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# API 서버
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Upload Worker
python -m worker.background_worker

# Vision Worker
python -m worker.vision_worker

# (선택) PC Folder Watcher
python -m watcher.folder_watcher
```

## Repository 목록 (Freeze)

| Repository | 주요 기능 | 사용 위치 |
|------------|-----------|-----------|
| UploadJobRepository | create/get/mark/count | Upload API, UploadWorker, Dashboard |
| VisionJobRepository | create/next/mark/count | UploadWorker, VisionWorker, Dashboard |
| MetadataRepository | save/get with priority | Plugins |
| TagRepository | save_ai_tag/save_user_tag | VisionPlugin, Gallery |
| HistoryRepository | metadata history | Metadata/Plugin failure |
| ApiUsageRepository | can_use/increase | Gps/Vision, Dashboard |
| GeocodeCacheRepository | find/save | GpsPlugin |
| WorkerStatusRepository | heartbeat/status | Workers, Dashboard |
| GalleryRepository | list/detail/search/map/timeline/statistics | Gallery API |

## Version Policy

- **1.0.0**: API / DB / Worker / Plugin Freeze
- MemoryKeeper V2 기능은 별도 버전에서 확장
- 1.0.x 내 호환성 깨는 변경 금지

## QA / Performance Tests (운영 DB 사용 금지)

성능·병렬 스크립트는 운영 Postgres / PhotoPlatform에 쓰지 않는다.

필수 환경변수:

```bash
set TEST_DATABASE_URL=postgresql://user:pass@host:5432/tc_backend_test
set PHOTO_PLATFORM_ROOT_TEST=D:/tmp/PhotoPlatformTest
```

대상 스크립트: `scripts/qa_perf_phase1.py`, `qa_perf_phase2.py`, `qa_phase3a.py`  
운영 DB 정리(기본 dry-run):

```bash
python scripts/cleanup_perf_test_data.py --keep-ids 2,3,4
# 실제 삭제 전 백업 후:
# pg_dump -h HOST -p PORT -U USER -d DB -F c -f tc_backend_backup.dump
python scripts/cleanup_perf_test_data.py --execute --confirm-backup --keep-ids 2,3,4
```
