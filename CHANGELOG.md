# CHANGELOG

## [1.0.0] - 2026-08-04

TC-Backend Version 1.0 Freeze.

### Added
- Common Upload API (`POST /api/common/upload`)
- Gallery Query API (list / detail / search / map / timeline / statistics)
- Monitoring API (`/api/common/health`, `/api/common/dashboard`)
- API Keys CRUD (`/api/common/api-keys`)
- UploadWorker Plugin Pipeline (Hash → Preview → Storage → Metadata → Exif → Gps)
- VisionWorker + Vision Queue (`common_vision_jobs`)
- Google Vision Label Detection + AI Tag 정책
- Storage Rule Engine (MemoryKeeper path rules)
- API Usage / Geocode Cache
- Worker Heartbeat Monitor (`common_worker_status`)
- PC Folder Watcher (`watcher/`)
- Docs: Architecture, API Reference, ERD, Plugin/Worker guides

### Fixed / Stabilized
- OpenAPI descriptions and response models
- `VERSION=1.0.0` exposed on Health / Dashboard / Workers / FastAPI

### Notes
- AstroJournal StorageRule / additional workers (OCR/Face/Plate) are reserved for later versions
- No breaking API changes intended within 1.0.x
