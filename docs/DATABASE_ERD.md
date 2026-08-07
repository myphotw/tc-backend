# Database ERD (v1.0.0 Freeze)

SQLAlchemy Model 기준. 물리 DB는 PostgreSQL.

## ER Diagram

The Astro Gallery read model joins `astro_observation_records.file_id` to
`common_files.id` and requires an `AstroJournal` row in `common_file_services`.
It is a query projection only and introduces no additional table.

```mermaid
erDiagram
    common_files ||--o| common_file_metadata : has
    common_files ||--o{ common_file_tags : has
    common_files ||--o{ common_metadata_history : has
    common_files ||--o{ common_vision_jobs : queues
    common_files ||--o{ common_file_services : linked_to
    common_files ||--o{ astro_observation_records : observed_as

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
| tag | NOT NULL, index |
| tag_type | Enum AI/ASTRO/USER/SYSTEM |
| source | Enum AI/USER |
| deleted | NOT NULL, index |

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
