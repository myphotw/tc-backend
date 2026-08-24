# MemoryKeeper Tag Curation V1

Google Vision `LABEL_DETECTION` 결과는 `common_file_tags`의 AI row에 영어
label/confidence 그대로 보존한다. `MemoryKeeperTagCurationService`는 이 raw
데이터를 읽어 사용자-facing projection만 계산하며 Google Vision 재호출이나 DB
재저장을 수행하지 않는다. 정책 버전은 `CURATION_VERSION = 1`이다.

## Pipeline

1. confidence 70 미만 제외
2. 색상·재질·촬영 특성 등 작은 low-value 예외 집합 제외
3. positive vocabulary에 있는 label만 canonical cluster로 변환
4. 같은 cluster/한국어 display 중복 제거, 구체 tag가 일반 tag 억제
5. structured metadata 및 USER tag/삭제 tombstone과 의미가 같으면 AI projection 제외
6. category별 기본 1개(강한 person/food/nature는 2개), 전체 최대 5개 선택
7. `canonical`/`aliases`는 검색용 내부 값, `display_name`/`tag`는 한국어 표시

의미 있는 결과가 없으면 0개가 정상이다. mapping이 없는 label은 raw DB에는
남지만 MemoryKeeper 사용자-facing 결과에는 영어 fallback으로 노출하지 않는다.
Vision 저장과 USER 할당은 raw AI row를 삭제하지 않으며 두 source의 우선순위는
projection에서만 결정한다.

## Unified catalog and identity

일반 UI는 `/api/memorykeeper/tags/catalog`에서 USER master와 active curated AI를
하나의 목록으로 조회한다. AI는 `ai:{canonical}`, USER-managed tag는 `tag:{id}`를
identity로 사용한다. usage count는 identity별 distinct MemoryKeeper file 수이다.

`mk_tag_canonical_overrides`는 canonical identity를 USER tag에 연결하거나
suppression한다. AI rename은 raw label/file relation을 바꾸지 않고 이 링크를
생성한다. 새 이름이면 USER tag를 만들거나 rename하고, normalized 이름이 이미
있으면 기존 tag로 자동 merge한다. 삭제는 링크를 suppression으로 남겨 다음
read-time curation에서 즉시 되살아나지 않게 한다. Gallery projection과 검색도
동일 링크를 사용하므로 catalog usage 범위와 검색 범위가 일치한다.

## Global suppression and file-level suppression

`mk_tag_canonical_overrides.suppressed=true`는 canonical을 모든 MemoryKeeper
사진에서 숨기는 전역 Catalog 정책이다. 사진 한 장의 태그 제거에는 사용하지
않는다. `mk_file_tag_suppressions`는 `(file_id, canonical_key)`를 유일하게 저장해
해당 사진의 사용자-facing projection만 숨긴다. display name이나 현재의
`ai:`/`tag:` identity 문자열이 아니라 stable canonical을 저장하므로 rename 이후도
유지된다. merge 시 source/target canonical suppression을 같은 파일에 전파한다.

파일별 처리 순서는 raw Vision → curation → global override → USER projection →
file suppression이다. USER relation과 AI canonical이 같은 화면 태그로 합쳐져
있다면 파일 DELETE는 USER relation tombstone과 canonical suppression을 함께
적용해 AI fallback을 막는다. raw AI label/confidence와 Vision job 결과는 바뀌지
않고, Vision 재처리도 suppression을 해제하지 않는다.

API는 다음 두 mutation을 제공한다.

- `DELETE /api/memorykeeper/files/{file_id}/tags/catalog/{identity}?expected_revision=n`
- `POST /api/memorykeeper/files/{file_id}/tags/catalog/{identity}` with
  `{ "expected_revision": n }`

POST는 기존 태그 선택/추가 UX의 restore 역할이다. 두 mutation 모두
`metadata_revision`을 검사·증가시키며 `file_id`, `identity`, `hidden`, 새
`revision`을 반환하고 `MemoryKeeperFileTag` change event를 남긴다. Gallery
detail/PhotoDetail projection, tag/keyword/alias 검색과 Catalog distinct-file
`usage_count`는 같은 파일 suppression을 사용한다. MemoryKeeper 파일 삭제 시
suppression은 물리 asset cleanup과 분리된 tombstone으로 남는다. AstroJournal-only
파일은 404이고 shared file의 AstroJournal raw projection은 영향을 받지 않는다.

Gallery detail의 MemoryKeeper 요청은 `ai_tags`를 curated 한국어로 반환하고,
`tags`에 USER 우선 통합 목록을 추가한다. 기존 `user_tags`는 유지한다.
AstroJournal 요청의 기존 raw `ai_tags` 의미는 변경하지 않는다. MemoryKeeper
Gallery `tag`/`keyword` 검색은 한국어 display/alias와 영어 canonical/raw alias를
같은 cluster로 확장한다.

## Existing data preview

다음 명령은 schema sync를 호출하지 않으며 PostgreSQL transaction을 `READ ONLY`로
설정한다. raw tag, USER tag, NAS storage를 수정하지 않는다.

```bash
python scripts/dry_run_memorykeeper_tag_curation.py --top 20
```

출력에는 기존/curated 평균·median, 0~5개 분포, 제거 비율, 자주 제거된 raw
label, mapping 검토 후보, 자주 남은 한국어 tag와 통합 catalog 고유 identity,
distinct usage, display 중복, canonical별 raw label 통합 결과가 포함된다. vocabulary 보강 후
같은 raw row에 새 curation version을 적용하면 되므로 Vision API 재호출은 없다.

## Missing FK preparation

운영 `common_file_tags.memorykeeper_tag_id`의 실제 FK 누락은 curation 동작과
분리한다. 적용 후보는 `scripts/migrate_memorykeeper_tag_fk.sql`이다.

- 적용 직전 PostgreSQL logical/physical backup과 복구 검증이 필요하다.
- orphan preflight 결과가 0이 아니면 중단하고 업무 규칙에 따라 별도 정리한다.
- `ADD ... NOT VALID`도 짧은 catalog lock이 필요하므로 저트래픽 시간에 실행한다.
- `VALIDATE CONSTRAINT`는 테이블을 스캔하고 I/O를 사용하지만 일반 DML을 막는
  강한 장기 lock은 피한다. 데이터량과 replica lag를 사전 관찰한다.
- 이번 변경에서는 SQL을 실행하지 않는다.

## Next UI work

Tag UI는 Gallery detail의 `tags`만 하나의 목록으로 표시하고 source별 화면을
요구하지 않아도 된다. 다음 단계에서는 전체 사용자-facing tag 집계/rename/merge
projection과 curation version·Vision job 상태를 보여주는 설정/상태 endpoint를
별도 계약으로 설계한다.
