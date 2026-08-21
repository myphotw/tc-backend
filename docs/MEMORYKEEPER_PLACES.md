# MemoryKeeper Places

`memorykeeper_places` is a MemoryKeeper-only domain for user-managed,
representative memory locations. It is not shared with AstroJournal observation
locations.

## API

- `GET/POST /api/memorykeeper/places`
- `GET/PATCH/DELETE /api/memorykeeper/places/{id}`
- `POST /api/memorykeeper/places/match`
- `POST /api/memorykeeper/places/radius-impact`
- `POST /api/memorykeeper/places/{id}/reclassify`
- `PATCH /api/memorykeeper/files/{file_id}/place`

All routes use the backend Bearer authentication inherited from the protected
API router. Place updates and file assignment use revision-based optimistic
locking.

## Matching and display

Automatic matching considers active places in this order: provider place ID,
canonical name, then the nearest center whose per-place radius contains the
photo. Equal-distance candidates use ascending place UUID for deterministic
selection.

If a MemoryKeeper-linked photo still has no match, the backend performs one
Google Places Legacy Nearby Search within 1,500m. Candidate ranking favors
meaningful place types (for example natural features, attractions, parks,
campgrounds and museums), then uses distance and provider rating signals.
Low-information generic establishments do not pass the POI threshold merely
because they are closest. The fallback order is district/city/province, raw
reverse-geocoded address, then a GPS label. Automatically created places use a
200m radius, `creation_source=AUTO_*`, and a unique internal deduplication key.

Raw EXIF GPS and reverse-geocoded fields remain unchanged. MemoryKeeper display
name resolution is `memorykeeper_places.display_name`, then raw `place_name`,
then `미분류`. AstroJournal responses do not project MemoryKeeper place fields.
An explicit `USER` assignment or unlink is authoritative and is never replaced
by the upload worker's automatic matcher.

## Dry-run-first migration

```text
python scripts/migrate_memorykeeper_places.py --input tb_place.json --dry-run
python scripts/backfill_memorykeeper_places.py --dry-run
python scripts/backfill_memorykeeper_places.py --dry-run --create-missing
```

Execution requires the explicit `--execute` switch. Legacy place GUIDs are
preserved by the importer. These commands must first be run against a test or
staging database and reviewed before any NAS execution.
