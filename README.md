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
| MemoryKeeper Reset | [docs/MEMORYKEEPER_RESET.md](docs/MEMORYKEEPER_RESET.md) |
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

AstroJournal additionally applies `DELETE_IF_UNREFERENCED` after committing the
record tombstone. If no active Astro record, non-Astro service link, or active
Vision processing job still protects the FileAsset, only its exact
`original`/`preview`/`thumb` paths are removed and the Astro service link is
cleaned up. The `common_files` row remains as a deleted tombstone because soft-
deleted records retain its FK; a later identical upload safely restores that
row and its media. MemoryKeeper uses `PRESERVE_PHYSICAL_FILE`, so a shared
MemoryKeeper link always prevents physical deletion. Storage cleanup failures
are logged and never roll back the ObservationRecord delete or its sync event.

## Changes Cursor API (B6-01)

`GET /api/common/changes` exposes append-only common change events ordered by a
monotonic cursor. Query parameters are `cursor` (exclusive, default `0`),
`limit` (`1..500`), and optional `service_name`. ObservationRecord CREATE,
UPDATE, and soft DELETE write events in the same transaction as the mutation.
DELETE events set `tombstone=true`; clients advance using `next_cursor` while
`has_more` is true.

## MemoryKeeper Semantic Reset

`POST /api/memorykeeper/reset/preview` and `/execute` support the PC client's
"start organization again" flow. Reset removes only MemoryKeeper service links,
places, user tags, suppressions, per-file favorite/memo and related projection
state. It never deletes `common_files` or original/preview/thumbnail assets and
does not alter AstroJournal records or links. Execute requires the literal
confirmation `RESET_MEMORYKEEPER` and is blocked while a MemoryKeeper upload or
MemoryKeeper-only Vision job is processing. See
`docs/MEMORYKEEPER_RESET.md` for the complete preservation and retry policy.

## External API Centralization (Phase 1)

Server-side Geocoding, Places, Weather, and Astrometry.net access is available
through normalized Backend endpoints. Enabled `common_api_keys` rows take
priority over `.env` fallbacks, and API key responses never expose plaintext or
ciphertext. Google Maps mobile SDK keys remain app deployment credentials;
Google Vision keeps its service-account JSON file flow. See
`docs/API_REFERENCE.md` and `docs/SECURITY.md` for contracts and deployment
boundaries.

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

### Photo metadata maintenance backfill

`ExifReader` supports capture dates stored in the nested Exif IFD. The common
metadata schema intentionally normalizes EXIF names: `DateTimeOriginal`, then
`DateTimeDigitized`, then `DateTime` become `datetime_original`; `Make`,
`Model`, and `LensModel` become `camera_make`, `camera_model`, and `lens`.
Reverse-geocoded formatted address is stored as `place_name`.

Inspect MemoryKeeper files without changing DB, storage, or external API usage:

```shell
python -m scripts.backfill_photo_metadata --service MemoryKeeper --dry-run
```

Inspect one operating file by its original filename:

```shell
python -m scripts.backfill_photo_metadata --service MemoryKeeper --filename 20260815_140628.jpg --dry-run
```

After reviewing the aggregate result, explicitly apply blank-only EXIF and
geography fields:

```shell
python -m scripts.backfill_photo_metadata --service MemoryKeeper --execute
```

Dry-run reads originals and the current geocode cache only; it does not call a
provider. Execute mode reuses the cache first and uses the configured Google
Geocoding resolver only for cache misses. The script refuses paths outside the
configured original root, records changes in metadata history, and does not
alter assets, hashes, raw GPS, registered Place relations, previews,
thumbnails, tags, favorites, Vision usage, or Vision jobs. The EXIF-only
`scripts/backfill_exif_metadata.py` entry point remains available for backward
compatibility.

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
- `GET /api/common/changes`
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

## MemoryKeeper 자동 태그

Google Vision 영어 label/confidence는 `common_file_tags`에 raw로 보존한다.
MemoryKeeper Gallery는 versioned curation policy로 최대 5개의 한국어 자동 태그와
USER 우선 통합 `tags` projection을 반환한다. AstroJournal의 raw tag 응답은
변경하지 않는다. 정책, read-only dry-run, FK 준비 절차는
[docs/MEMORYKEEPER_TAG_CURATION.md](docs/MEMORYKEEPER_TAG_CURATION.md)를 참고한다.

MemoryKeeper 태그 관리 UI는 `GET /api/memorykeeper/tags/catalog`의 통합
catalog만 사용한다. AI/USER를 구분하지 않고 `identity`, `display_name`,
`usage_count`, `revision`을 받으며, 같은 identity의 PATCH는 rename-or-merge,
DELETE는 USER relation 제거 또는 AI canonical suppression으로 처리된다.

사진 한 장에서 통합 태그를 숨길 때는 전역 Catalog DELETE가 아니라
`DELETE /api/memorykeeper/files/{file_id}/tags/catalog/{identity}`를 사용한다.
`expected_revision`은 파일의 `metadata_revision`이며, 성공 응답은 증가한
`revision`을 반환한다. 다시 추가할 때는 같은 경로의 POST를 사용한다. 파일별
suppression은 `mk_file_tag_suppressions`에 stable canonical key로 남으므로 Catalog
rename/merge 또는 Vision 재처리 후에도 유지되며 AstroJournal projection에는
적용되지 않는다.

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
| `VISION_MONTHLY_LIMIT` | `900` | Vision 월간 요청 상한. 무료 1000 unit 중 100 unit을 보호 버퍼로 남기며 900을 초과해 설정해도 유효 상한은 900 |
| `GEOCODING_MONTHLY_LIMIT` | `100000` | Geocoding 월간 unit |
| `WEATHER_MONTHLY_LIMIT` | `100000` | Weather 월간 unit |
| `PLATESOLVE_MONTHLY_LIMIT` | `100000` | Platesolve 월간 unit |

## 실행 방법

```bash
# 의존성
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Development / regression tests
pip install -r requirements-dev.txt

# API 서버
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Upload Worker
python -m worker.background_worker

# Vision Worker
python -m worker.vision_worker

# (선택) PC Folder Watcher
python -m watcher.folder_watcher
```

### Portable setup (Windows / Docker / NAS)

Copy `.env.example` to `.env` and set local values. The `.env` file and the
actual Vision service-account JSON must remain untracked. A virtual environment
is machine-specific; recreate `.venv` after moving the repository instead of
copying it from another PC.

For Docker Compose, `PHOTO_PLATFORM_HOST_PATH` and
`GOOGLE_VISION_CREDENTIAL_HOST` are host-side paths. Keep their machine- or
NAS-specific values only in the local `.env`; containers always use
`/data/PhotoPlatform` and `/run/secrets/google-vision.json`. Start the optional
Vision worker with the `vision` profile only after mounting a real credential:

```bash
docker compose up -d backend upload-worker
docker compose --profile vision up -d vision-worker
```

The tracked watcher configuration uses `./watcher_data/incoming`. Override
`watch_paths` and `upload_api_base_url` in an ignored local copy when the watcher
runs on another host; do not commit machine-specific paths:

```bash
python -m watcher.folder_watcher watcher_data/watch_config.local.json
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
set PHOTO_PLATFORM_ROOT_TEST=./watcher_data/qa-photo-platform
```

대상 스크립트: `scripts/qa_perf_phase1.py`, `qa_perf_phase2.py`, `qa_phase3a.py`  
운영 DB 정리(기본 dry-run):

```bash
python scripts/cleanup_perf_test_data.py --keep-ids 2,3,4
# 실제 삭제 전 백업 후:
# pg_dump -h HOST -p PORT -U USER -d DB -F c -f tc_backend_backup.dump
python scripts/cleanup_perf_test_data.py --execute --confirm-backup --keep-ids 2,3,4
```
