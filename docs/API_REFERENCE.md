# API Reference (v1.0.0 Freeze)

Base URL: `http://<host>:8000`  
OpenAPI: `/docs`, `/openapi.json`  
Version: `1.0.0`

## External API Centralization (Phase 1)

All provider credentials remain server-side. Provider response bodies are
normalized and are never passed through verbatim.

### Geocoding and Places

| Method | Endpoint | Query |
|--------|----------|-------|
| GET | `/api/common/geocoding/reverse` | `latitude`, `longitude`, optional `language=ko` |
| GET | `/api/common/geocoding/forward` | `query`, optional `language=ko` |
| GET | `/api/common/places/autocomplete` | `query`, optional `language`, `session_token` |
| GET | `/api/common/places/details` | `place_id`, optional `language`, `session_token` |
| GET | `/api/common/places/search` | `query`, optional `language` |

Forward geocoding, Place Details and Text Search return normalized location
items with `display_name`, `latitude`, `longitude`, `country`, `province`,
`city`, `district`, `place_name`, `provider`, and optional `place_id`.
Autocomplete returns `place_id`, `main_text`, `secondary_text`, and
`display_name`. When the app uses an autocomplete billing session, it must send
the same opaque `session_token` to Autocomplete and Place Details.

Reverse geocoding reuses `common_geocode_cache`. A cache hit does not consume a
usage unit. Actual successful Geocoding and Places provider calls consume one
unit; mock calls and provider failures do not.

### Weather

| Method | Endpoint | Query |
|--------|----------|-------|
| GET | `/api/common/weather/current` | `lat`, `lon`, optional `language=ko` |
| GET | `/api/common/weather/forecast` | `lat`, `lon`, optional `language=ko` |

Weather uses OpenWeatherMap metric units. Current weather includes normalized
temperature, feels-like, humidity, pressure, cloud, wind, condition,
visibility, observation, sunrise and sunset fields. Forecast returns normalized
timestamped slots including precipitation probability and rain volume.

### Plate Solve

`POST /api/astro/plate-solve` accepts:

```json
{"common_file_id": 180}
```

The file must be an active `common_files.id` linked to `AstroJournal`. The
Backend reuses its original media and does not upload the image from the app a
second time. A successful submission returns HTTP `202`:

```json
{
  "job_id": "opaque-token",
  "status": "WAITING",
  "common_file_id": 180,
  "provider": "astrometry.net",
  "result": null,
  "provider_metadata": {"submission_id": 123}
}
```

`GET /api/astro/plate-solve/{job_id}` returns `WAITING`, `PROCESSING`,
`COMPLETED`, or `FAILED`. A completed result contains `ra`, `dec`, `rotation`,
`pixel_scale`, `field_width`, `field_height`, and `parity`. Phase 1 uses an
encrypted stateless token backed by the provider submission rather than a new
database table or worker queue.

### External API errors

Errors use FastAPI `detail` with a stable `code`: `API_KEY_NOT_CONFIGURED`
(`503`), `API_LIMIT_EXCEEDED` (`429`), `PROVIDER_TIMEOUT` (`504`),
`PROVIDER_ERROR` (`502`), or `INVALID_REQUEST` (`400`). Raw provider bodies,
request URLs containing credentials, and key values are not returned.

### Capability and readiness

`GET /api/common/capabilities` describes supported contracts. Runtime key and
Vision worker state is separate at `GET /api/common/readiness`. Readiness only
reports whether a key is configured and whether its source is `database` or
`environment`; it never returns key material.

## Shared FileAsset domain links (B2)

The upload contract continues to accept `service_name`. Files with the same
SHA-256 share one `common_files` FileAsset and are associated with each service
through `common_file_services`. This does not alter existing upload or Gallery
response fields. Gallery collection endpoints apply `service_name` using the
domain link, so a shared asset can appear in both MemoryKeeper and AstroJournal
collections without duplicating its physical storage.

## AstroJournal Observation Records (B3)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/astro/records` | Create an AstroJournal observation for an existing `common_files.id`. |
| GET | `/api/astro/records` | List active records; supports `catalog_object_id`, `favorite`, `representative`. |
| GET | `/api/astro/records/{record_id}` | Read one active record. |
| PATCH | `/api/astro/records/{record_id}` | Update with required `revision`; stale revisions return `409 Conflict`. |
| DELETE | `/api/astro/records/{record_id}` | Soft delete the record. |

`representative=true` requires `catalog_object_id`; the service clears any other
active representative for the same catalog object. The response includes
`revision`, timestamps, plate-solve status, and FileAsset FK. These endpoints do
not change MemoryKeeper API behavior.

## AstroJournal Gallery Projection (B4-01)

Common Gallery is the FileAsset read API. Astro Gallery is the canonical
`ObservationRecord + FileAsset` projection and does not add Astro-specific
fields to `/api/common/gallery*`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/astro/gallery` | List active Astro observations with FileAsset media URLs. |
| GET | `/api/astro/gallery/{record_id}` | Read one active projected observation; otherwise `404`. |

List query parameters are `page`, `page_size`, `catalog_object_id`, `favorite`,
`date_from`, and `date_to`. Results sort by `captured_at DESC`, then
`created_at DESC`. Records are excluded when the ObservationRecord is soft
deleted, the FileAsset is deleted or missing, or the FileAsset has no
`AstroJournal` domain link. Media URLs reuse Common Gallery routes and are null
when the corresponding storage path is absent.

## AstroJournal Mutation Contract (B5-01)

`POST /api/astro/records` accepts optional UUID `client_record_id`. Replaying
the same `AstroJournal + client_record_id` returns the existing record; request
field differences on a replay do not overwrite it.

`PATCH /api/astro/records/{record_id}` requires `revision` and supports partial
updates to `catalog_object_id`, `captured_at`, latitude/longitude,
`location_name`, `equipment_id`, `memo`, `favorite`, and `representative`.
The existing `plate_solve_status` field remains accepted for B3 compatibility;
this ticket adds no Plate Solve processing. Omitted fields are unchanged. A
stale revision returns:

```json
{
  "detail": {
    "code": "REVISION_CONFLICT",
    "record_id": "2a3b6bce-b169-4b17-91b9-c09913735741",
    "expected_revision": 3,
    "current_revision": 4
  }
}
```

`DELETE /api/astro/records/{record_id}` performs an idempotent soft delete and
returns `record_id`, `deleted`, `revision`, and `deleted_at`. The first delete
increments revision; subsequent requests return the existing deletion result.

## Changes Cursor API (B6-01)

`GET /api/common/changes` returns changes whose event cursor is strictly greater
than the supplied `cursor`.

| Query | Default | Description |
|-------|---------|-------------|
| `cursor` | `0` | Exclusive non-negative event cursor. |
| `limit` | `100` | Page size from 1 through 500. |
| `service_name` | none | Optional `MemoryKeeper` or `AstroJournal` filter. |

```json
{
  "items": [
    {
      "cursor": 42,
      "service_name": "AstroJournal",
      "resource_type": "ObservationRecord",
      "resource_id": "2a3b6bce-b169-4b17-91b9-c09913735741",
      "operation": "DELETE",
      "revision": 4,
      "tombstone": true,
      "changed_at": "2026-08-07T12:00:00Z"
    }
  ],
  "next_cursor": 42,
  "has_more": false
}
```

CREATE and UPDATE events identify the resource and revision; clients retrieve
the current canonical resource separately. DELETE is represented as a
tombstone. An empty page retains the requested cursor as `next_cursor`.

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

AstroJournal uploads additionally accept these optional multipart fields:

| Field | Format | Storage use |
|-------|--------|-------------|
| `observation_date` | ISO date (`YYYY-MM-DD`) | Observation year; preferred over EXIF/upload time. |
| `canonical_target_id` | String, max 255 characters | Canonical target folder key. |
| `target_display_name` | String, max 255 characters | Fallback when the canonical ID is absent. |

These fields only determine the original storage path. They do not create or
update an AstroJournal `ObservationRecord`.

For the same `service_name + client_file_id`, the API returns the original job
without saving another incoming file. Different non-null hashes return `409`.
If an earlier job has no hash, a later valid hash backfills it and returns the
original job. File-only requests retain the legacy response shape.

Extended upload and Upload Job status responses distinguish these identifiers:

| Field | Type | Meaning |
|-------|------|---------|
| `backend_file_id` | string or null | SHA-256-backed logical file identifier (`common_files.file_id`). |
| `common_file_id` | integer or null | `common_files.id` database PK used by `ObservationRecord.file_id`. |

`common_file_id` is normally null while the Job is WAITING and becomes
available after the worker creates or resolves the shared `CommonFile`.

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
  "places": "OK",
  "astrometry": "OK",
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
| PATCH | `/api/common/api-keys/{key_id}` | 200 | key/description/enabled 갱신 |
| DELETE | `/api/common/api-keys/{key_id}` | 200 | 삭제 |

Request (`POST`): `{ "service_name", "api_key", "description?", "enabled?" }`.
Supported service names are `GOOGLE_GEOCODING`, `GOOGLE_PLACES`, `WEATHER`, and
`ASTROMETRY`. Read/write responses expose `configured`, `enabled`, metadata,
and `masked="****"`; they never expose stored ciphertext or plaintext.

---

## Vision Queue / Worker (HTTP 아님)

| Component | Entry | Notes |
|-----------|-------|-------|
| UploadWorker | `python -m worker.background_worker` | UploadJob → Plugins → Vision Queue |
| VisionWorker | `python -m worker.vision_worker` | VisionJob → VisionPlugin |
| Folder Watcher | `python -m watcher.folder_watcher` | PC 폴더 → Upload API |

Vision Queue 테이블: `common_vision_jobs`  
상태: `WAITING` → `PROCESSING` → `COMPLETED` / `FAILED`
