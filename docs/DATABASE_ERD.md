# Database ERD (v1.0.0 Freeze)

SQLAlchemy Model 기준. 물리 DB는 PostgreSQL.

## ER Diagram

The Astro Gallery read model joins `astro_observation_records.file_id` to
`common_files.id` and requires an `AstroJournal` row in `common_file_services`.
It is a query projection only and introduces no additional table.

MemoryKeeper semantic reset also introduces no table or column. It deletes the
`MemoryKeeper` edges and MemoryKeeper-owned projection rows shown below while
preserving the `common_files` node, common raw metadata/tag rows and every
AstroJournal edge/record. A single `MemoryKeeperReset` row is appended to the
existing `common_change_events` feed.

```mermaid
erDiagram
    common_files ||--o| common_file_metadata : has
    common_files ||--o{ common_file_tags : has
    common_files ||--o{ common_metadata_history : has
    common_files ||--o{ common_vision_jobs : queues
    common_files ||--o{ common_file_services : linked_to
    common_files ||--o{ astro_observation_records : observed_as
    common_files ||--o| memorykeeper_file_states : has_memorykeeper_state
    common_files ||--o{ mk_file_tag_suppressions : suppresses_for_memorykeeper
    mk_tags ||--o{ common_file_tags : catalogs
    mk_tags ||--o{ mk_tag_canonical_overrides : overrides

    common_files {
        int id PK
        string file_id UK
        string original_name
        string extension
        string mime_type
        bigint file_size
        int width
        int height
        text original_path
        text preview_path
        text thumb_path
        bool favorite
        string service_name
        datetime created_at
        datetime updated_at
        bool deleted
    }

    common_file_services {
        int id PK
        int file_id FK
        string service_name
        datetime created_at
        datetime updated_at
    }

    memorykeeper_file_states {
        int file_id PK_FK
        bool favorite
        text memo
        int revision
        datetime created_at
        datetime updated_at
    }

    mk_tags {
        int id PK
        string tag_name UK
        string normalized_name UK
        string tag_type
        string source
        bool favorite
        int revision
        bool deleted
        datetime created_at
        datetime updated_at
    }

    mk_tag_canonical_overrides {
        int id PK
        string canonical_key UK
        int memorykeeper_tag_id FK
        bool suppressed
        int revision
        datetime created_at
        datetime updated_at
    }

    mk_file_tag_suppressions {
        int id PK
        int file_id FK
        string canonical_key
        int revision
        bool deleted
        datetime created_at
        datetime updated_at
    }

    astro_observation_records {
        uuid id PK
        int file_id FK
        string service_name
        uuid client_record_id
        string catalog_object_id
        datetime captured_at
        float latitude
        float longitude
        string location_name
        string equipment_id
        text memo
        bool favorite
        bool representative
        string plate_solve_status
        datetime created_at
        datetime updated_at
        datetime deleted_at
        int revision
    }

    common_change_events {
        bigint id PK
        string service_name
        string resource_type
        string resource_id
        string operation
        int revision
        bool tombstone
        datetime changed_at
    }

    common_file_metadata {
        int id PK
        int file_id UK_FK
        string camera_make
        string camera_model
        string lens
        datetime datetime_original
        float gps_lat
        float gps_lon
        float gps_alt
        string country
        string city
        string place_name
        string reserved
        bool locked
    }

    common_file_tags {
        int id PK
        int file_id FK
        int memorykeeper_tag_id FK
        string tag
        enum tag_type
        enum source
        float confidence
        bool deleted
    }

    common_upload_jobs {
        int id PK
        string job_id UK
        string source_type
        string status
        text incoming_path
        string file_id
        int retry_count
        text processing_log
    }

    common_vision_jobs {
        int id PK
        int file_id FK
        int priority
        enum status
        int retry_count
        enum vision_provider
        bool deleted
    }

    common_api_usage {
        int id PK
        enum provider
        string api_name
        int year
        int month
        int used_unit
        int limit_unit
        int remaining_unit
    }

    common_geocode_cache {
        int id PK
        float latitude
        float longitude
        string country
        string city
        string place_name
    }

    common_metadata_history {
        int id PK
        int file_id FK
        string field_name
        text old_value
        text new_value
        string source
        int priority
    }

    common_worker_status {
        string worker_name PK
        string status
        datetime last_started
        datetime last_heartbeat
        int processed_count
        int failed_count
        string current_job_id
        string version
    }
```

## Table Details

### common_files
| Column | Constraints |
|--------|-------------|
| id | PK, index |
| file_id | UNIQUE, NOT NULL, index (SHA256) |
| favorite | NOT NULL, default false, index |
| service_name | NOT NULL, default MemoryKeeper, index |
| deleted | NOT NULL, default false |

### common_file_metadata
| Column | Constraints |
|--------|-------------|
| id | PK |
| file_id | FK → common_files.id, UNIQUE |
| datetime_original / gps_* / location / camera / astro_* | nullable |
| locked | NOT NULL default false |

### common_file_tags
| Column | Constraints |
|--------|-------------|
| id | PK |
| file_id | FK → common_files.id, index |
| memorykeeper_tag_id | nullable FK 모델 → mk_tags.id, index; 운영 실제 constraint는 별도 검증 필요 |
| tag | NOT NULL, index |
| tag_type | Enum AI/ASTRO/USER/SYSTEM |
| source | Enum AI/USER |
| deleted | NOT NULL, index |

### mk_tag_canonical_overrides
| Column | Constraints |
|--------|-------------|
| id | PK |
| canonical_key | UNIQUE, stable curated AI identity |
| memorykeeper_tag_id | nullable FK → mk_tags.id, index |
| suppressed | NOT NULL default false, index |
| revision | NOT NULL default 1 |

`canonical_key → memorykeeper_tag_id`는 AI tag rename/merge의 USER override이고,
`memorykeeper_tag_id=NULL, suppressed=true`는 raw Vision row를 보존한 suppression이다.

### mk_file_tag_suppressions
| Column | Constraints |
|--------|-------------|
| id | PK |
| file_id | FK → common_files.id, index |
| canonical_key | stable curated semantic identity, index |
| revision | NOT NULL default 1 |
| deleted | NOT NULL default false, index; true는 restore/file-delete tombstone |

`UNIQUE(file_id, canonical_key)`는 같은 파일/semantic identity의 중복 suppression을
막는다. 이 테이블은 MemoryKeeper read projection에만 적용된다. 전역 정책인
`mk_tag_canonical_overrides.suppressed`와 독립적이고 raw `common_file_tags` AI row를
수정하지 않는다. 신규 테이블이므로 schema sync의 `create_all`에서 생성되며 기존
데이터 backfill은 필요 없다.

### common_upload_jobs
| Column | Constraints |
|--------|-------------|
| id | PK |
| job_id | UNIQUE UUID |
| status | index (WAITING/PROCESSING/COMPLETED/FAILED) |
| file_id | nullable SHA256 string, index |

### common_vision_jobs
| Column | Constraints |
|--------|-------------|
| id | PK |
| file_id | FK → common_files.id, index |
| priority | index |
| status | Enum WAITING/PROCESSING/COMPLETED/FAILED/SKIPPED |
| vision_provider | Enum GOOGLE/AZURE/AWS/LOCAL |

### common_api_usage
| Column | Constraints |
|--------|-------------|
| id | PK |
| provider+api_name+year+month | UNIQUE |
| used_unit / limit_unit / remaining_unit | NOT NULL |

### common_geocode_cache
| Column | Constraints |
|--------|-------------|
| id | PK |
| latitude+longitude | UNIQUE |
| latitude / longitude | index |

### common_metadata_history
| Column | Constraints |
|--------|-------------|
| id | PK |
| file_id | FK → common_files.id, index |
| field_name | index |

### common_worker_status
| Column | Constraints |
|--------|-------------|
| worker_name | PK |
| status | index |
| last_heartbeat | index |
| version | nullable (Worker VERSION) |

## MemoryKeeper Reset Boundary

Reset delete/clear order follows existing FK direction:

1. Block active MemoryKeeper upload and MemoryKeeper-only PROCESSING Vision jobs.
2. Disable non-completed MemoryKeeper-only Vision jobs; preserve completed/shared jobs.
3. Clear only `common_file_metadata.memorykeeper_place_*` projection columns.
4. Delete `mk_file_tag_suppressions` and `mk_tag_canonical_overrides`.
5. Delete MemoryKeeper USER `common_file_tags`, legacy `mk_photo_tags`, then `mk_tags`.
6. Delete `memorykeeper_file_states` and `memorykeeper_places`.
7. Delete MemoryKeeper upload idempotency history and `common_file_services` links.
8. Append one `MemoryKeeperReset` event and commit.

The transaction never updates `common_files.deleted`, never deletes
`common_file_metadata`, raw AI `common_file_tags`, `common_vision_jobs` COMPLETED
results, `astro_observation_records`, or AstroJournal/other service links. No
filesystem path is resolved or removed by Reset.
