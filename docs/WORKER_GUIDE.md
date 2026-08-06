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
  → ApiUsageRepository.can_use(VISION)
      초과: WAITING 유지 + 30분 재시도 (종료하지 않음)
  → PROCESSING
  → VisionPlugin
  → COMPLETED / FAILED (+ retry_count)
```

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
