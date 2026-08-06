# Plugin Guide (v1.0.0 Freeze)

Plugin은 `BasePlugin` 상속 시 Registry에 자동 등록된다.  
Worker는 `PluginManager.load_plugins(worker_scope)`만 호출한다.

## Plugin Priority 표

| plugin_name | version | priority | worker_scope | 역할 |
|-------------|---------|----------|--------------|------|
| HashPlugin | 1.0.0 | 10 | upload | SHA256, 중복 검사 |
| PreviewPlugin | 1.0.0 | 20 | upload | preview / thumb 생성 |
| StoragePlugin | 1.0.0 | 30 | upload | StorageRuleEngine 경로 + move + common_files |
| MetadataPlugin | 1.0.0 | 40 | upload | 기본 metadata 저장 |
| ExifPlugin | 1.0.0 | 50 | upload | EXIF 추출 |
| GpsPlugin | 1.0.0 | 60 | upload | Geocode cache / reverse geocode |
| VisionPlugin | 1.0.0 | 70 | vision | Google Vision Label → AI Tag |

## 공통 속성

- `plugin_name`
- `plugin_version` (= `1.0.0`)
- `plugin_priority` (낮을수록 먼저)
- `worker_scope` (`upload` | `vision`)
- `enabled` (기본 true)
- `PluginContext.plugin_enabled` 로 disable 가능

## Logging

```
PLUGIN_START <Name> v1.0.0
...
PLUGIN_COMPLETE <Name> v1.0.0
```

실패 시 `PLUGIN_FAILED` + Metadata History(`plugin_error`).

## Storage Rule

StoragePlugin은 Path를 직접 만들지 않고:

```
StorageRuleEngine.build_path(context)
→ MemoryKeeper: year/country/city/place_name
→ 부족 시 Unknown
```

AstroJournal Rule은 인터페이스만 준비 (미구현).

## Vision Tag 정책

- AI Tag 자동 저장 (`source=AI`)
- USER Tag 존재 시 동일 의미 AI Tag 생성 안 함
- 승인(approved) 없음
