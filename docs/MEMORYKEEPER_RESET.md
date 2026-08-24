# MemoryKeeper Semantic Reset

## Purpose

MemoryKeeper Reset implements the PC client's "처음부터 다시 구성" workflow. It
returns the MemoryKeeper domain projection to an empty state so the user can
import the same originals and classify them again. It is not a database-wide
wipe and **is not an original-photo deletion feature**.

The PC client's local SQLite wipe remains a separate client operation. Backend
Reset does not move that local implementation into TC-Backend.

## API workflow

1. Call Bearer-protected `POST /api/memorykeeper/reset/preview` with no body.
2. Show the affected and preserved counts to the user.
3. After explicit UI confirmation, call `POST /api/memorykeeper/reset/execute`
   with `{ "confirmation": "RESET_MEMORYKEEPER" }`.
4. On success, clear all MemoryKeeper caches and retain the returned
   `reset_event_cursor` as part of normal change-feed synchronization.
5. The user may import the original files again. SHA-256 reuses each CommonFile.

Preview performs queries only. Execute is one database transaction; any error
rolls back every semantic mutation.

## Reset and preservation boundary

Reset removes:

- MemoryKeeper rows in `common_file_services`;
- MemoryKeeper per-file favorite/memo state;
- MemoryKeeper Place relations and the Place master;
- MemoryKeeper USER file-tag relations and USER tag master;
- file-level tag suppressions and canonical rename/merge/suppression overrides;
- MemoryKeeper-only semantic metadata history;
- old MemoryKeeper upload job/idempotency rows, allowing intentional re-import;
- non-completed Vision jobs that have no service consumer after the link removal.

Reset preserves:

- every `common_files` row, SHA-256 and `deleted` value;
- original, preview and thumbnail paths and physical files;
- raw EXIF, GPS and reverse-geocoding metadata;
- raw Vision AI labels and confidence;
- completed Vision jobs/results and project-wide Vision usage;
- AstroJournal and other service links;
- AstroJournal ObservationRecords, including shared-file records;
- shared Vision jobs still required by another service;
- the append-only historical change feed.

The legacy `common_files.service_name` compatibility column is not used as the
Reset ownership boundary and is not modified. Ownership is determined only by
`common_file_services`.

## Worker and race policy

Execute returns `409 MEMORYKEEPER_RESET_BLOCKED` while a MemoryKeeper WAITING or
PROCESSING upload exists, or while a MemoryKeeper-only Vision job is PROCESSING.
Waiting uploads are also a blocker because they could recreate a service link
immediately after Reset.

PostgreSQL uses a transaction-scoped advisory read/write guard: concurrent
MemoryKeeper uploads take a shared lock and Reset takes the exclusive lock.
Existing service/job rows are additionally row-locked. Vision WAITING claim is a
conditional update requiring `status=WAITING AND deleted=false`; a stale worker
therefore cannot claim a job that Reset has just disabled. Shared AstroJournal
Vision processing is not blocked or modified.

## Vision reuse and quota

The common raw result is keyed by the reused CommonFile. On duplicate
MemoryKeeper import:

- an active COMPLETED Vision job means the result is reused, including a valid
  zero-label result;
- active raw AI labels are reused even if an old completion marker is absent;
- an existing WAITING/PROCESSING job is reused;
- only when no reusable result or active job exists is a new WAITING job created.

Preview's `preserved_raw_vision_count` counts distinct files with raw labels or
an active COMPLETED result, so a valid zero-label completion is included.

Reset never deletes raw results to force reanalysis. Any genuinely required new
job still passes the project-wide atomic 900-unit hard cap; Reset provides no
quota bypass.

## Change feed and client behavior

Execute emits one `MemoryKeeperReset` UPDATE event rather than thousands of
per-file tombstones. The event is scoped to `service_name=MemoryKeeper` and does
not appear in an AstroJournal-filtered feed. MemoryKeeper clients must treat it
as a full invalidation signal for Gallery, Home, Visit, Travel, Tags, Pending and
Places projections.

The old append-only events are retained for cursor monotonicity and audit. A
client reading from an older cursor may see earlier row events followed by the
Reset event; the final high-level event wins and triggers full invalidation.
