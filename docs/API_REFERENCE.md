# API Reference (v1.0.0 Freeze)

Base URL: `http://<host>:8000`  
OpenAPI: `/docs`, `/openapi.json`  
Version: `1.0.0`

## Authentication

When `TC_BACKEND_AUTH_TOKEN` is configured, the informational root and every
`/api/*` operation require the standard header:

```http
Authorization: Bearer <TC Backend token>
```

This includes capabilities/readiness, upload and polling, Common and Astro
Gallery, Changes, ObservationRecord mutations, API-key administration,
Geocoding, Places, Weather, and Plate Solve. Missing, empty, malformed, Basic,
or incorrect credentials return HTTP `401`:

```json
{
  "detail": {
    "code": "UNAUTHORIZED",
    "message": "Authentication required"
  }
}
```

The response also includes `WWW-Authenticate: Bearer`. Public operational
exceptions are only `GET /health` and `GET /db-test`; their responses do not
include credentials, configuration paths, or database connection details.
An unset token retains LAN development compatibility, but must not be used for
external deployment.

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
second time. A successful queue request returns HTTP `202`:

```json
{
  "job_id": "6de34c79-a067-43ec-9bd6-e683f5926c73",
  "status": "WAITING",
  "common_file_id": 180,
  "provider": "astrometry.net",
  "result": null,
  "provider_metadata": {"submission_id": null, "provider_job_id": null}
}
```

`GET /api/astro/plate-solve/{job_id}` returns `WAITING`, `PROCESSING`,
`COMPLETED`, or `FAILED`. A completed result contains `ra`, `dec`, `rotation`,
`pixel_scale`, `field_width`, `field_height`, and `parity`.

New ObservationRecords automatically enqueue one persistent Plate Solve job per
numeric `common_files.id`. The POST endpoint creates or reuses the same queue
row and never starts a new stateless provider submission. `PlateSolveWorker`
claims the job and performs the Astrometry submit/poll outside database
transactions. Queue job IDs are UUIDs; the GET endpoint also continues to
accept encrypted Phase 1 tokens that were issued before the persistent queue.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/astro/plate-solve/summary` | Counts `total`, `WAITING`, `PROCESSING`, `COMPLETED`, `FAILED`. |
| POST | `/api/astro/plate-solve/{job_id}/retry` | Moves a persisted `FAILED` job back to `WAITING`. |

### External API errors

Errors use FastAPI `detail` with a stable `code`: `API_KEY_NOT_CONFIGURED`
(`503`), `API_LIMIT_EXCEEDED` (`429`), `PROVIDER_TIMEOUT` (`504`),
`PROVIDER_ERROR` (`502`), or `INVALID_REQUEST` (`400`). Raw provider bodies,
request URLs containing credentials, and key values are not returned.

### AstroJournal Astronomy Events

#### `GET /api/astro/events`

Returns a normalized upcoming-event list sourced from SpaceCatalog. The
provider's raw response, query vocabulary, descriptions, URLs, attribution, and
circumstance payload are not exposed to AstroJournal.

| Query | Default | Contract |
|-------|---------|----------|
| `from` | current UTC date at `00:00:00Z` | Timezone-aware ISO-8601 start. |
| `to` | six calendar months after `from` | Timezone-aware ISO-8601 end; later than `from` and at most two years after it. |

```json
{
  "events": [
    {
      "id": "shower-perseids-2027-08-12",
      "type": "meteor_shower",
      "title": "페르세우스자리 유성우",
      "start_at": "2027-07-17T19:17:40Z",
      "peak_at": "2027-08-12T22:34:27Z",
      "end_at": "2027-08-24T09:08:06Z",
      "tags": ["맨눈 관측", "광시야 촬영"],
      "priority": 90
    }
  ]
}
```

Normalized types are `meteor_shower`, `solar_eclipse`, `lunar_eclipse`,
`planet_viewing`, and `conjunction`. `start_at`/`end_at` come from a shower's
provider window and are null for instant events; `peak_at` is always the
provider's UTC instant. No D-Day display string is returned.

Only showers with provider `circumstances.zhr >= 10` are included. Moon
quarters, seasons, asteroid flybys, unsupported records, completed events, and
duplicate IDs are omitted. Results sort by `peak_at` ascending. Solar eclipse
tags are `보호장비 필수` and `촬영 추천`; they never recommend naked-eye
observation.

Successful responses are held in a process-local cache for 24 hours per query
range. An expired matching entry is returned when SpaceCatalog is unavailable;
for the moving default range, the latest successful entry may be used as stale
fallback. With no usable cache, errors follow the common provider contract
(`429`, `502`, or `504`). The cache is lost on process restart. SpaceCatalog
requires no API key and this endpoint adds no DB state.

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

## MemoryKeeper Fast Gallery and Travel reads

`GET /api/memorykeeper/gallery/photos` keeps SHA-256 `file_id` as the Common
Gallery media identity and returns numeric `common_file_id` separately. Media
URLs are emitted only when the corresponding persisted derivative path is
present. `GET /api/common/gallery/{file_id}/thumbnail` may serve the persisted
preview when the thumbnail file has drifted from storage; it never falls back
to the original or performs an on-demand resize.

`GET /api/memorykeeper/travel/aggregates` remains a two-query set-based
projection. Place items additionally expose nullable `latitude` and
`longitude`, preferring the active `memorykeeper_places` coordinates and then
a deterministic complete raw GPS pair from `common_file_metadata`. No
per-place query is performed.

`GET /api/memorykeeper/travel/memories` preserves `exact_anniversary` and
`previous_year_period` and adds a bounded `items` projection plus
`past_year_period` and `long_ago`. Each item has an optional `category` of
`EXACT_ANNIVERSARY`, `PREVIOUS_YEAR_PERIOD`, `PAST_YEAR_PERIOD`, or `LONG_AGO`.
`items` is deduplicated by numeric `common_file_id`, selected round-robin across
available categories, and limited by the existing `limit` query parameter. All
candidates refer to real active MemoryKeeper FileAssets and canonical
`effective_capture_date` values. `LONG_AGO` is limited to assets from at least
two calendar years before the reference year.

## AstroJournal Observation Records (B3)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/astro/records` | Create an AstroJournal observation for an existing `common_files.id`. |
| GET | `/api/astro/records` | List active records; supports `catalog_object_id`, `favorite`, `representative`. |
| GET | `/api/astro/records/{record_id}` | Read one active record with Plate Solve job/result projection. |
| PATCH | `/api/astro/records/{record_id}` | Update with required `revision`; stale revisions return `409 Conflict`. |
| DELETE | `/api/astro/records/{record_id}` | Soft delete the record. |

`representative=true` requires `catalog_object_id`; the service clears any other
active representative for the same catalog object. The response includes
`revision`, timestamps, plate-solve status, and FileAsset FK. These endpoints do
not change MemoryKeeper API behavior.

The record detail additionally returns `plate_solve_job_id` and
`plate_solve_result`. The result is populated only for a persisted `COMPLETED`
job; all other job states return null result fields. A record without a job
keeps its existing `plate_solve_status` and returns null job/result.

## AstroJournal Gallery Projection (B4-01)

Common Gallery is the FileAsset read API. Astro Gallery is the canonical
`ObservationRecord + FileAsset` projection and does not add Astro-specific
fields to `/api/common/gallery*`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/astro/gallery` | List active observations with media URLs plus Plate Solve status/job ID. |
| GET | `/api/astro/gallery/{record_id}` | Read one projection with persisted Plate Solve result; otherwise `404`. |

List query parameters are `page`, `page_size`, `catalog_object_id`, `favorite`,
`date_from`, and `date_to`. Results sort by `captured_at DESC`, then
`created_at DESC`. Records are excluded when the ObservationRecord is soft
deleted, the FileAsset is deleted or missing, or the FileAsset has no
`AstroJournal` domain link. Media URLs reuse Common Gallery routes and are null
when the corresponding storage path is absent.

Both projections join the Plate Solve job by numeric
`ObservationRecord.file_id = astro_plate_solve_jobs.common_file_id`. SHA
`common_files.file_id` retains its existing media identity meaning. List items
include `plate_solve_status` and `plate_solve_job_id`; only detail responses add
`plate_solve_result` (`ra`, `dec`, `rotation`, `pixel_scale`, `field_width`,
`field_height`, `parity`). The job ID is returned as an opaque string suitable
for the existing retry endpoint.

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

After that soft delete and its change tombstone are committed, AstroJournal
applies a separate physical cleanup policy. The FileAsset is preserved while
another active Astro record, any non-Astro service link (including
MemoryKeeper), or an active Vision processing job references it. Otherwise the
Backend validates and deletes only the exact `original`, `preview`, and `thumb`
paths below `PHOTO_PLATFORM_ROOT`, removes Astro's domain link and non-history
derived data, and marks the `common_files` row deleted with cleared asset paths.
The row remains as an FK tombstone for deleted records and can be restored by a
later upload of the same SHA-256.

Missing asset files count as already cleaned. Unsafe/out-of-root paths and
partial I/O failures are logged for retry and do not roll back record deletion.
The DELETE response schema is unchanged; clients continue to rely only on
`record_id`, `deleted`, `revision`, and `deleted_at`. MemoryKeeper retains its
physical-file preservation policy.

## AstroJournal Capture Data Reset

Both endpoints use the common Bearer authentication dependency.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/astro/reset/preview` | Read-only ownership, job guard and physical-delete impact. |
| POST | `/api/astro/reset/execute` | Execute full AstroJournal capture-data Reset. |

Execute requires `{"confirmation":"RESET_ASTROJOURNAL"}`. Invalid
confirmation returns `422`. A PROCESSING Astro upload or Astro-only Vision job
or Plate Solve job returns `409 ASTROJOURNAL_RESET_BLOCKED`. Astro-only media is
removed and its CommonFile becomes a tombstone. Shared media, metadata,
MemoryKeeper state and other links are preserved. Astro upload/idempotency rows
are removed, allowing same-ID and byte-identical re-registration.

One `AstroJournalReset` change event invalidates the complete capture
projection. The current Reset contract does not yet delete or count persistent
Plate Solve queue/result rows, so its Plate Solve counts remain zero.
PhotoObject is not implemented. See
[ASTROJOURNAL_RESET.md](ASTROJOURNAL_RESET.md).

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

## MemoryKeeper file writes, tags and pending

All endpoints below use the protected Bearer-authenticated API router.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| DELETE | `/api/memorykeeper/files/{file_id}` | Unlink MemoryKeeper and clean an unreferenced physical file |
| PATCH | `/api/memorykeeper/files/{file_id}/metadata` | Update favorite, memo and raw geography with optimistic locking |
| GET/POST | `/api/memorykeeper/tags` | List/create the user tag catalog |
| PATCH/DELETE | `/api/memorykeeper/tags/{tag_id}` | Rename, favorite or delete a tag |
| POST | `/api/memorykeeper/tags/{tag_id}/merge` | Merge a source tag into a target tag |
| POST/DELETE | `/api/memorykeeper/files/{file_id}/tags/{tag_id}` | Assign/remove a user tag |
| POST/DELETE | `/api/memorykeeper/files/{file_id}/tags/catalog/{identity}` | Restore/hide one unified tag on one file |
| GET | `/api/memorykeeper/pending` | List files without a registered MemoryKeeper Place |
| POST | `/api/memorykeeper/pending/assign-place` | Assign one Place to multiple pending files atomically |

`MemoryKeeperFileMetadataUpdate.expected_revision` protects favorite, memo and
raw geography writes. Blank memo/address strings are normalized to `null`.
`gps_lat` and `gps_lon` must be supplied together. Raw geography remains common
photo metadata, while favorite/memo are stored in the MemoryKeeper-only file
state so shared AstroJournal records are not changed.

Pending is derived from `common_file_metadata.memorykeeper_place_id IS NULL`;
GPS and reverse-geocoded address values do not make a file complete. Gallery
list/search accept `incomplete=true|false` for the same projection.
Pending suggestions are optional (`include_suggestions=true`) and reuse only
the existing registered Place matcher; the default list performs no provider
lookup and no automatic Place creation.

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
- `incomplete` (optional; MemoryKeeper derived Place state)

### List Item Schema

`file_id`, `filename`, `preview_url`, `thumbnail_url`, `capture_datetime`, `country`, `city`, `place_name`, `camera_model`, `favorite`, `memo`, `metadata_revision`, `incomplete`, `has_gps`, `has_ai_tag`, `service_name`

### Search Query

`year`, `country`, `city`, `camera`, `tag`, `favorite`, `incomplete`, `service_name`, `date_from`, `date_to`, `keyword`, `page`, `page_size`, `sort`

### Detail Schema

Metadata 전체, `ai_tags`, `user_tags`, 통합 `tags`, `storage_path`, preview/thumbnail/original URL, `history_count`

- `service_name=MemoryKeeper`: `ai_tags`는 curation V1의 한국어 자동 태그이며
  `tags`는 USER 우선 사용자-facing 통합 목록이다. 자동 태그에는
  `canonical`, `display_name`, `aliases`, `curation_version`이 포함된다.
- `service_name=AstroJournal`: 기존 `ai_tags` raw label 의미를 유지한다.
- MemoryKeeper의 `tag`/`keyword` 검색은 한국어 display/alias 및 영어
  canonical/raw alias를 같은 의미 cluster로 검색한다.

### MemoryKeeper Unified Tag Catalog

기존 `/api/memorykeeper/tags` USER CRUD는 호환용으로 유지한다. 일반 UI는
source를 노출하지 않는 다음 additive API를 사용한다.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/memorykeeper/tags/catalog` | USER + curated AI 통합 목록 |
| PATCH | `/api/memorykeeper/tags/catalog/{identity}` | rename, 동일 이름이면 자동 merge |
| DELETE | `/api/memorykeeper/tags/catalog/{identity}` | USER 삭제 또는 AI canonical suppression |

Catalog item은 `identity`, `display_name`, `usage_count`, `favorite`, `revision`,
`editable`, `canonical_references`를 반환하며 `source`는 consumer contract에 없다.
PATCH body는 `name`, `revision`이다. AI identity는 `ai:{canonical}`, USER-managed
identity는 `tag:{id}`이며 mutation 결과가 USER-managed identity로 승격될 수 있다.

### MemoryKeeper file-level tag visibility

Catalog DELETE는 모든 MemoryKeeper 파일에 적용되는 전역 정책이다. 사진 한 장의
표시 태그를 제거할 때는 반드시 아래 file-level API를 사용한다.

| Method | Endpoint | Input | Result |
|--------|----------|-------|--------|
| DELETE | `/api/memorykeeper/files/{file_id}/tags/catalog/{identity}` | query `expected_revision` | 해당 파일에서 identity 숨김 |
| POST | `/api/memorykeeper/files/{file_id}/tags/catalog/{identity}` | JSON `{ "expected_revision": n }` | suppression 해제 및 identity 복원/할당 |

두 응답은 `file_id`, 요청한 `identity`, `hidden`, 새 `revision`을 반환한다.
`expected_revision`은 Gallery의 `metadata_revision`과 같은 optimistic-lock 영역이며
불일치하면 기존 `REVISION_CONFLICT` 409를 반환한다. USER relation과 같은 의미의
AI canonical이 함께 있으면 DELETE는 USER relation을 tombstone 처리하는 동시에
canonical을 파일 단위로 숨겨 refresh 시 AI가 다시 나타나지 않게 한다.

처리 순서는 raw Vision → curation → global canonical override → USER projection →
file suppression → 최종 `tags`이다. raw Vision row/confidence/Vision job은 변경하지
않는다. Gallery detail, PhotoDetail이 사용하는 detail projection, `tag`/`keyword`
검색 및 Catalog `usage_count`가 같은 suppression을 사용한다. POST restore 또는
기존 numeric USER-tag assign은 연결 가능한 canonical suppression을 해제한다.
mutation은 `MemoryKeeperFileTag` change event를 생성하며 DELETE는 tombstone이다.
MemoryKeeper service link가 없으면 404이고, shared file의 AstroJournal raw
projection에는 영향을 주지 않는다.

### MemoryKeeper automatic-tag operations

설정 화면은 공통 운영 Dashboard의 DB 용어를 해석하지 않고 다음 Bearer 보호 API를
사용한다. 모든 집계와 retry 대상은 현재 `MemoryKeeper` service link가 있는 파일로
제한되며 AstroJournal-only job은 제외된다.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/memorykeeper/auto-tags/status` | credential, worker, queue, 월 사용량과 curation 상태 |
| GET | `/api/memorykeeper/auto-tags/failed` | 안전하게 redaction된 실패 목록; `page`, `page_size` |
| POST | `/api/memorykeeper/auto-tags/retry-failed` | retry 가능한 FAILED를 WAITING으로 재큐잉; `limit` 기본 100, 최대 500 |
| POST | `/api/memorykeeper/auto-tags/jobs/{job_id}/retry` | 같은 내부 정책으로 한 job 재시도 |
| GET | `/api/memorykeeper/auto-tags/curation-preview` | raw label을 변경하지 않는 제한형 preview; `sample_limit` 기본 200, 최대 500 |

Status 필드 의미:

- `credential_ready`: 설정된 Google Vision credential 파일이 존재한다.
- `worker_online`: `VisionWorker` heartbeat가 기존 60초 online 기준을 만족한다.
- `service_available`: credential과 worker가 준비되었는지를 뜻한다. quota 고갈은
  서비스 장애로 취급하지 않으며 이 값 대신 아래 quota 필드로 구분한다. 일부
  FAILED job의 존재만으로 false가 되지도 않는다.
- `quota_available`: 현재 월에 예약 가능한 Vision unit이 1 이상인지 나타낸다.
- `monthly_limit_reached`: 유효 월 상한에 도달했으면 true다.
- `quota_waiting_count`: 상한 도달 중인 MemoryKeeper 범위 WAITING job 수다.
- `waiting_count`, `processing_count`, `failed_count`: MemoryKeeper 범위의 활성 job 수.
- `today_completed_count`, `last_processed_at`, `last_failure_at`: UTC 기준 처리 상태.
- `monthly_usage`: 현재 월 `common_api_usage`의 실제 Vision 호출 unit.
- `monthly_limit`: 서버 설정과 무관하게 900을 넘지 않는 유효 상한. 현재 Google
  Vision 무료 1000 unit 중 100 unit은 결제 방지용 보호 버퍼다. UI는 이 값을
  하드코딩하지 않는다.
- `monthly_remaining`: `max(0, monthly_limit - monthly_usage)`.
- `curation_version`: 현재 read-time curation 정책 버전.

Failed 목록은 public SHA-256 `file_id`, numeric `job_id`, `failed_at`,
`retry_count`, `safe_error_code`, `retryable`만 반환한다. exception stack, provider
response, credential path/token과 원본 `last_error`는 반환하지 않는다. retry는 최대
실패 3회이며 성공/PROCESSING/WAITING job은 skip한다. usage limit pre-check에 걸린
job은 FAILED가 되지 않고 WAITING과 기존 retry count를 그대로 유지한다. retry는
raw AI tag와 성공 결과를 먼저 지우지 않는다.

현재 Vision 기능은 Google Cloud Vision `LABEL_DETECTION`만 호출하며 사진 한 장의
한 번 분석을 1 unit으로 예약한다. `common_api_usage`는 service별로 나뉘지 않은
`GOOGLE/VISION/year/month` 행이므로 MemoryKeeper와 AstroJournal 호출을 합산한다.
Worker의 사전 잔여량 검사는 빠른 대기 판단용이며, 실제 provider 호출 직전 DB의
조건부 UPDATE로 unit을 원자적으로 예약한다. 동시 worker가 마지막 한 unit을
관찰해도 하나만 900번째 unit을 예약할 수 있다. 예약에 실패한 job은 FAILED로
바뀌지 않고 WAITING, 기존 `retry_count`, raw tag를 유지한다. 월 키가 UTC
`year/month`이므로 다음 달에는 별도 reset 작업 없이 새 월 사용량으로 자동
재개한다. provider가 예약 후 실패한 경우에는 안전을 위해 예약 unit을 반환하지
않는다.

Curation V1은 raw Vision row에서 매 조회 시 계산되므로 별도의 재정리 mutation은
없다. Preview는 전체 raw file/tag count를 가벼운 aggregate로 계산하고 최대
`sample_limit` 파일만 현재 규칙으로 평가한다. `evaluated_file_count`, `has_more`로
sample 범위를 표시하며 `projected_curated_tag_count`, `zero_tag_file_count`,
`mapped_percentage`는 평가 sample 기준이다. Catalog override/file suppression을
변경하지 않는 순수 read-only curation-stage preview다.
따라서 preview는 Vision quota가 소진되어도 호출 가능하고 외부 API unit을
소비하지 않는다.

Vision 성공 시 MemoryKeeper service link가 있으면 기존 `MemoryKeeperFileTag`
UPDATE change event를 생성하여 Gallery/PhotoDetail/Catalog 캐시를 갱신할 수 있다.
이번 계약은 MemoryKeeper 전용 ON/OFF나 적게/보통/많이 정책을 추가하지 않는다.
공통 Vision queue가 AstroJournal에도 사용되므로 global OFF는 허용하지 않으며,
현재 0~5개 유용한 태그 정책을 설정 개수를 위해 확장하지 않는다.

### MemoryKeeper semantic reset

Reset은 MemoryKeeper PC의 "처음부터 다시 구성"을 위한 MemoryKeeper-only semantic
초기화다. **원본 사진 삭제 기능이 아니다.** 두 endpoint 모두 기존 Bearer 인증을
요구한다.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/memorykeeper/reset/preview` | 현재 영향과 보존 범위를 read-only로 계산 |
| POST | `/api/memorykeeper/reset/execute` | 명시적 확인 후 하나의 DB transaction으로 실행 |

Preview body는 없으며 다음 필드를 반환한다.

- 초기화 규모: `memorykeeper_file_count`, `place_count`, `user_tag_count`,
  `favorite_count`, `memo_count`, `file_tag_relation_count`,
  `file_tag_suppression_count`, `pending_count`, `upload_job_count`.
- 보존 규모: `preserved_common_file_count`, `preserved_raw_vision_count`,
  `shared_with_other_service_count`.
- 실행 차단: `active_upload_job_count`, `processing_vision_job_count`,
  `reset_blocked`.

`preserved_raw_vision_count`는 raw AI label이 있거나 COMPLETED 분석 이력이 있는
MemoryKeeper 파일 수다. 정상적인 zero-label 완료 결과도 재사용 대상으로 포함한다.

Execute request:

```json
{ "confirmation": "RESET_MEMORYKEEPER" }
```

다른 값이나 필드 누락은 `422`다. 성공 응답은 `reset_completed`,
`affected_file_count`, `removed_place_count`, `removed_user_tag_count`,
`cleared_state_count`, `preserved_common_file_count`,
`preserved_raw_vision_count`, `reset_event_cursor`를 반환한다.

MemoryKeeper `WAITING`/`PROCESSING` upload job 또는 다른 service link가 없는
MemoryKeeper-only `PROCESSING` Vision job이 있으면 실행하지 않고 다음 `409`를
반환한다.

```json
{
  "detail": {
    "code": "MEMORYKEEPER_RESET_BLOCKED",
    "active_upload_job_count": 1,
    "processing_vision_job_count": 0
  }
}
```

실행은 MemoryKeeper service link, per-file favorite/memo, Place relation/master,
USER tag relation/master, file suppression, canonical override, MemoryKeeper semantic
history와 이전 MemoryKeeper upload idempotency job을 제거한다. 기존 upload job을
제거하는 이유는 의도적인 재등록이 이전 `client_file_id` replay에 가로막히지 않게
하기 위해서다. CommonFile, SHA-256, raw metadata, raw AI label/confidence, 완료된
Vision 결과, 물리 asset, AstroJournal link/ObservationRecord는 보존한다.

다른 service 소비자가 없는 MemoryKeeper-only WAITING/FAILED Vision job은 reset 후
불필요한 Google 호출을 막기 위해 비활성화한다. shared/Astro job과 COMPLETED 결과는
보존한다. 동일 SHA 재등록 시 완료/raw 결과가 있으면 재사용하고, 결과가 전혀 없을
때만 새 WAITING job을 만든다. 모든 변경은 단일 transaction이며 마지막에
`MemoryKeeperReset` high-level change event 하나를 기록한다. 클라이언트는 이 event로
Gallery/Home/Visit/Travel/Tags/Pending/Places cache 전체를 invalidate해야 한다.

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
