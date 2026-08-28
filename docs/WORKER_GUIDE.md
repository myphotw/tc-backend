# Worker Guide (v1.0.0 Freeze)

## UploadWorker

Entry: `python -m worker.background_worker`

```
UploadJob WAITING
  → Hash(10)
  → Preview(20)
  → Storage(30)  # StorageRuleEngine + move
  → Metadata(40)
  → Exif(50)
  → Gps(60)
  → Vision Queue enqueue (common_vision_jobs)
  → COMPLETED
```

Monitor:
- start → `RUNNING`
- heartbeat 30초
- success/fail 카운트
- stop → `STOPPED`
- heartbeat 60초 초과 → Dashboard `OFFLINE`

Version: `settings.VERSION` (`1.0.0`)

## VisionWorker

Entry: `python -m worker.vision_worker`

```
common_vision_jobs WAITING
  → priority DESC, requested_at DESC
  → ApiUsageRepository.can_use(VISION)  # advisory pre-check
      초과: WAITING 유지 + 30분 재시도 (종료하지 않음)
  → PROCESSING
  → VisionPlugin → provider 호출 직전 reserve_usage(VISION)
      예약 실패/동시 경합: WAITING 복귀, retry_count 유지
  → COMPLETED / FAILED (+ retry_count)
```

Vision 유효 월 상한은 최대 900 unit이다. Google 무료 1000 unit 중 100 unit을
보호 버퍼로 남기며 설정값이 더 작으면 그 값을 사용한다. 사용량은
MemoryKeeper/AstroJournal 공통이고 UTC 월 변경 시 자동으로 새 월 행을 사용한다.
현재 `LABEL_DETECTION` 한 번을 1 unit으로 예약한다.

실패 로그: `VISION_FAILED`  
성공 로그: `VISION_COMPLETE`

## Vision Queue

| Field | Notes |
|-------|-------|
| file_id | FK common_files.id |
| priority | PriorityCalculator |
| status | WAITING/PROCESSING/COMPLETED/FAILED/SKIPPED |
| vision_provider | default GOOGLE |

Upload API는 Queue를 만들지 않는다. UploadWorker가 Metadata/EXIF/GPS 후 enqueue.

## PlateSolveWorker

Entry: `python -m worker.plate_solve_worker`

```
ObservationRecord CREATE
  → astro_plate_solve_jobs WAITING (common_file_id UNIQUE)
  → short claim transaction + lease
  → PROCESSING
  → Astrometry submit/poll (no DB transaction)
  → short result transaction
  → COMPLETED / FAILED
```

The canonical identity is numeric `common_files.id`; SHA `common_files.file_id`
is never used as the queue FK. Expired PROCESSING leases are reclaimable. A
reclaimed row with `provider_submission_id` resumes polling without uploading
the physical file again. Heartbeat and lease updates use independent short
sessions. Retry is explicit through the Plate Solve retry API and preserves the
attempt count. `POST /api/astro/plate-solve` only creates or reuses this queue;
legacy encrypted job IDs remain GET-only compatibility data.
Each worker uses `PlateSolveWorker-{hostname}-{pid}` by default so scaled
containers have distinct heartbeat and lease-owner identities.

## EXIF / GPS persistence and maintenance

신규 업로드는 `MetadataPlugin → ExifPlugin → GpsPlugin` 순서로 처리한다.
`ExifPlugin`은 EXIF 결과를 `common_file_metadata`에 commit하고, `GpsPlugin`은
기존 `common_geocode_cache`, `KeyResolver`, `GeocodingClient`를 재사용하여
`country/province/city/district/place_name`을 저장한다. Gallery와 PhotoDetail은
같은 metadata row를 projection한다.

기존 파일의 누락값은 기본 dry-run 관리 스크립트로 점검한다.

```shell
python -m scripts.backfill_photo_metadata --service MemoryKeeper --dry-run
python -m scripts.backfill_photo_metadata --service MemoryKeeper --execute
```

`--filename`과 `--limit`으로 범위를 제한할 수 있다. Dry-run은 provider를
호출하지 않으며 DB/스토리지/Vision 상태를 변경하지 않는다. Execute도
null/empty 필드만 채우고 raw GPS와 MemoryKeeper Place relation은 변경하지
않는다. Google의 formatted address는 현재 schema의 `place_name`에 저장한다.

## Folder Watcher (PC)

Entry: `python -m watcher.folder_watcher`

```
watchdog → stable size → SHA256 cache
  → skip duplicate
  → copy2 temp → POST /api/common/upload
```

기존 Upload API / Worker를 그대로 사용.

## Repository 사용 위치 (요약)

| Repository | Worker/API |
|------------|------------|
| UploadJobRepository | Upload API, UploadWorker, Dashboard |
| VisionJobRepository | UploadWorker, VisionWorker, Dashboard |
| MetadataRepository | Metadata/Exif/Gps/Vision plugins |
| TagRepository | VisionPlugin, Gallery |
| ApiUsageRepository | Gps/Vision, Dashboard |
| GeocodeCacheRepository | GpsPlugin |
| HistoryRepository | Metadata updates, plugin failure |
| WorkerStatusRepository | WorkerMonitor, Dashboard |
| GalleryRepository | Gallery API |
