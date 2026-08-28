# AstroJournal Capture Data Reset

AstroJournal Reset means **capture data initialization**. AstroJournal stores
user-processed capture results and is not the irreplaceable original-photo
archive. This differs from MemoryKeeper Reset, which never deletes NAS media.

## Ownership and deletion flow

```text
astro_observation_records
          |
          v
common_files (SHA-256 FileAsset)
          |
          +-- common_file_services: AstroJournal only
          |       -> delete original / preview / thumb
          |       -> delete common metadata and raw tags for the dead asset
          |       -> soft-delete Vision jobs
          |       -> remove Astro link and tombstone CommonFile
          |
          +-- AstroJournal + another service
                  -> delete Astro records and Astro link only
                  -> preserve CommonFile, media, metadata, tags and other links
```

The Reset also deletes all AstroJournal upload job/idempotency rows and their
safe incoming assets. This allows the same `client_file_id`, the same SHA-256,
or newly processed bytes to be uploaded again. A byte-identical Astro-only
asset restores the existing deleted CommonFile tombstone through the normal
Hash/Storage plugin flow.

MemoryKeeper links, file state, legacy photo data, media and every other
service's change events are preserved. API usage, credentials, settings and
worker status are outside the Reset boundary.

## Preview

`POST /api/astro/reset/preview` is read-only. Its response includes ownership,
upload/processing guard, and planned physical deletion counts.
`pending_upload_count` counts AstroJournal `WAITING` uploads.
`processing_job_count` combines AstroJournal upload processing, Astro-only
Vision processing, and Plate Solve processing.

Plate Solve now has a persistent queue/result table in addition to legacy
encrypted submission tokens. Reset integration is intentionally deferred:
`plate_solve_result_count` and `deleted_plate_solve_result_count` remain zero,
and queue/result rows are preserved. `PROCESSING` Plate Solve jobs block Reset;
WAITING jobs become `FAILED` so they cannot process tombstoned media after
Reset. This avoids silently destroying solve history until an explicit
retention policy is approved. `PhotoObject` is not
implemented, so `photo_object_count` is also zero.

## Execute

`POST /api/astro/reset/execute` requires:

```json
{"confirmation": "RESET_ASTROJOURNAL"}
```

Any other confirmation returns `422`. Both endpoints use the existing Backend
Bearer protection. Execute returns `409 ASTROJOURNAL_RESET_BLOCKED` while an
AstroJournal upload or Astro-only Vision job is `PROCESSING`.

PostgreSQL advisory and row locks serialize Reset against new AstroJournal
upload creation. Physical media is deleted first through the existing
contained-path cleanup service, then DB changes are staged in one transaction.
Filesystem unlink and a DB commit cannot be fully atomic. On failure, ownership
rows remain retryable; a later run treats already absent media as successful.

ObservationRecords are hard-deleted in one bulk operation because Reset is a
full semantic reinitialization, not a normal record DELETE. Reset preserves the
append-only cursor log and adds one monotonic invalidation event:

```text
service_name = AstroJournal
resource_type = AstroJournalReset
resource_id = AstroJournal
operation = UPDATE
```

Clients receiving it must invalidate the complete local AstroJournal capture
projection. No database migration is required.
