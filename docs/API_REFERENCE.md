# API Reference (v1.0.0 Freeze)

Base URL: `http://<host>:8000`  
OpenAPI: `/docs`, `/openapi.json`  
Version: `1.0.0`

신규 API는 v1.0 Freeze 이후 별도 버전으로 추가한다.

---

## System

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/` | 200 | 서비스 정보 (`service`, `status`, `version`) |
| GET | `/health` | 200 | 간단 health (`status`, `version`) |
| GET | `/db-test` | 200 | `SELECT 1` DB 연결 점검 |

---

## Upload

### Capability discovery

`GET /api/common/capabilities` returns API version `1.1`, supported services,
and enabled upload-contract fields. It is separate from runtime health.

### `POST /api/common/upload`

Optional multipart fields: `service_name` (`MemoryKeeper` default; allowed:
`MemoryKeeper`, `AstroJournal`), `client_file_id`, and
`client_content_sha256` (64-character lowercase hexadecimal SHA-256).

For the same `service_name + client_file_id`, the API returns the original job
without saving another incoming file. Different non-null hashes return `409`.
If an earlier job has no hash, a later valid hash backfills it and returns the
original job. File-only requests retain the legacy response shape.

- **Request**: `multipart/form-data` field `file`
- **Response 200**
  ```json
  {
    "id": 1,
    "job_id": "uuid",
    "status": "WAITING",
    "incoming_path": "incoming/uuid_name.jpg"
  }
  ```
- **400**: filename 누락
- **500**: 저장/DB 실패 (incoming 롤백 시도)

비고: 후처리는 UploadWorker. Vision Queue는 Worker가 생성.

---

## Gallery

Legacy Gallery list/search/map/timeline/statistics endpoints default to
`MemoryKeeper` when `service_name` is omitted. Supply
`service_name=AstroJournal` to query AstroJournal assets.

| Method | Endpoint | Response Model | Status |
|--------|----------|----------------|--------|
| GET | `/api/common/gallery` | `GalleryListResponse` | 200 |
| GET | `/api/common/gallery/{file_id}` | `GalleryDetailResponse` | 200 / 404 |
| GET | `/api/common/gallery/search` | `GallerySearchResponse` | 200 |
| GET | `/api/common/gallery/map` | `MapMarkerListResponse` | 200 |
| GET | `/api/common/gallery/timeline` | `TimelineResponse` | 200 |
| GET | `/api/common/gallery/statistics` | `StatisticsResponse` | 200 |

### List Query

- `page` (default 1)
- `page_size` (default 20, max 200)
- `sort` (default `capture_datetime_desc`)
- `service_name` (optional)

### List Item Schema

`file_id`, `filename`, `preview_url`, `thumbnail_url`, `capture_datetime`, `country`, `city`, `place_name`, `camera_model`, `favorite`, `has_gps`, `has_ai_tag`, `service_name`

### Search Query

`year`, `country`, `city`, `camera`, `tag`, `favorite`, `service_name`, `date_from`, `date_to`, `keyword`, `page`, `page_size`, `sort`

### Detail Schema

Metadata 전체, `ai_tags`, `user_tags`, `storage_path`, preview/thumbnail/original URL, `history_count`

### Map Item Schema

`file_id`, `latitude`, `longitude`, `place_name`, `thumbnail`, `year`, `service_name`

---

## Monitoring

### `GET /api/common/health`

**200**
```json
{
  "status": "OK",
  "version": "1.0.0",
  "database": "OK",
  "storage": "OK",
  "vision": "OK",
  "weather": "OK",
  "geocoding": "OK",
  "time": "ISO-8601"
}
```

### `GET /api/common/dashboard`

**200**: `version`, `upload`, `vision`, `api_usage`, `storage`, `workers[]`

Worker 항목: `name`, `status` (`RUNNING`/`STOPPED`/`OFFLINE`), `last_heartbeat`, `processed_today`, `failed_today`, `current_job_id`, `version`

---

## API Keys

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/api/common/api-keys/` | 200 | 목록 |
| POST | `/api/common/api-keys/` | 200 | 생성 (암호화 저장) |
| DELETE | `/api/common/api-keys/{key_id}` | 200 | 삭제 |

Request (`POST`): `{ "service_name", "api_key", "description?" }`

---

## Vision Queue / Worker (HTTP 아님)

| Component | Entry | Notes |
|-----------|-------|-------|
| UploadWorker | `python -m worker.background_worker` | UploadJob → Plugins → Vision Queue |
| VisionWorker | `python -m worker.vision_worker` | VisionJob → VisionPlugin |
| Folder Watcher | `python -m watcher.folder_watcher` | PC 폴더 → Upload API |

Vision Queue 테이블: `common_vision_jobs`  
상태: `WAITING` → `PROCESSING` → `COMPLETED` / `FAILED`
