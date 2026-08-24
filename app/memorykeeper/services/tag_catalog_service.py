from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.tag_repository import TagSource, TagType
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.models.tag_canonical_override import (
    MemoryKeeperTagCanonicalOverride,
)
from app.memorykeeper.schemas.tag import (
    TagCreate,
    TagMergeRequest,
    TagUpdate,
    UnifiedTagCatalogItem,
    UnifiedTagCatalogResponse,
    UnifiedTagRenameRequest,
)
from app.memorykeeper.services.tag_curation_service import (
    CuratedTag,
    MemoryKeeperTagCurationService,
    RawTagInput,
)
from app.memorykeeper.services.tag_service import MemoryKeeperTagService


@dataclass(frozen=True)
class ProjectedCuratedTag:
    identity: str
    canonical: str
    display_name: str
    confidence: float
    aliases: tuple[str, ...]
    curation_version: int
    source: str
    tag_id: int | None
    revision: int


@dataclass
class _CatalogEntry:
    identity: str
    display_name: str
    favorite: bool
    revision: int
    canonical_references: set[str] = field(default_factory=set)
    file_ids: set[int] = field(default_factory=set)

    def response(self) -> UnifiedTagCatalogItem:
        return UnifiedTagCatalogItem(
            identity=self.identity,
            display_name=self.display_name,
            usage_count=len(self.file_ids),
            favorite=self.favorite,
            revision=self.revision,
            editable=True,
            canonical_references=sorted(self.canonical_references),
        )


class MemoryKeeperTagCatalogService:
    """Unified USER + curated-AI catalog and mutation facade."""

    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.curation = MemoryKeeperTagCurationService()
        self.tags = MemoryKeeperTagService(db)

    def list(
        self,
        *,
        query: str | None,
        limit: int,
        offset: int,
    ) -> UnifiedTagCatalogResponse:
        entries = list(self._entries().values())
        if query:
            normalized = self.curation.normalize(query)
            entries = [
                entry
                for entry in entries
                if normalized in self.curation.normalize(entry.display_name)
                or any(
                    self._query_matches_canonical(normalized, canonical)
                    for canonical in entry.canonical_references
                )
            ]
        entries.sort(
            key=lambda entry: (
                not entry.favorite,
                -len(entry.file_ids),
                self.curation.normalize(entry.display_name),
                entry.identity,
            )
        )
        total = len(entries)
        return UnifiedTagCatalogResponse(
            items=[entry.response() for entry in entries[offset : offset + limit]],
            total=total,
        )

    def rename_or_merge(
        self,
        identity: str,
        payload: UnifiedTagRenameRequest,
    ) -> UnifiedTagCatalogItem:
        entry = self._require_entry(identity)
        self._check_revision(identity, payload.revision, entry.revision)
        normalized_name = self.curation.normalize(payload.name)
        source_tag_id = self._tag_id(identity)
        target = self.tags._find_normalized(normalized_name, include_deleted=False)
        target_canonicals: set[str] = set()
        if target is not None:
            target_entry = self._entries().get(f"tag:{target.id}")
            if target_entry is not None:
                target_canonicals.update(target_entry.canonical_references)
        target_rule = self.curation.rule_for(payload.name)
        if target_rule is not None:
            target_canonicals.add(target_rule.canonical)
        self._propagate_merged_suppressions(
            entry.canonical_references | target_canonicals
        )

        if source_tag_id is not None:
            source = self.tags.get(source_tag_id)
            if target is not None and target.id == source.id:
                return entry.response()
            self._ensure_canonical_links(
                entry.canonical_references,
                tag_id=source.id,
                suppressed=False,
            )
            if target is None:
                result = self.tags.update(
                    source.id,
                    TagUpdate(revision=payload.revision, name=payload.name),
                )
                target_id = result.id
            else:
                result = self.tags.merge(
                    source.id,
                    TagMergeRequest(
                        source_revision=payload.revision,
                        target_tag_id=target.id,
                        target_revision=target.revision,
                    ),
                )
                target_id = result.id
            self._ensure_canonical_links(
                entry.canonical_references,
                tag_id=target_id,
                suppressed=False,
            )
            self.db.commit()
            return self._require_entry(f"tag:{target_id}").response()

        target = self._ensure_target(payload.name, target)
        if identity.startswith("ai:"):
            canonical = identity.removeprefix("ai:")
            self._ensure_override(canonical, tag_id=target.id, suppressed=False)
        elif identity.startswith("legacy:"):
            self._promote_legacy(entry.display_name, target)
            self._ensure_canonical_links(
                entry.canonical_references,
                tag_id=target.id,
                suppressed=False,
            )
        else:
            self._not_found(identity)
        self.db.commit()
        return self._require_entry(f"tag:{target.id}").response()

    def delete(self, identity: str, *, expected_revision: int) -> None:
        entry = self._require_entry(identity)
        self._check_revision(identity, expected_revision, entry.revision)
        tag_id = self._tag_id(identity)
        if identity.startswith("ai:"):
            self._ensure_override(
                identity.removeprefix("ai:"),
                tag_id=None,
                suppressed=True,
            )
            self.db.commit()
            return
        if tag_id is not None:
            self._ensure_canonical_links(
                entry.canonical_references,
                tag_id=tag_id,
                suppressed=False,
            )
            self.tags.delete(tag_id, expected_revision=expected_revision)
            return
        if identity.startswith("legacy:"):
            normalized = self.curation.normalize(entry.display_name)
            for relation in self._legacy_relations(normalized):
                relation.deleted = True
            self.db.commit()
            return
        self._not_found(identity)

    def project_curated_tags(
        self,
        tags: list[CuratedTag] | tuple[CuratedTag, ...],
        *,
        file_id: int | None = None,
    ) -> list[ProjectedCuratedTag]:
        overrides = self._override_map()
        masters = self._active_tags()
        masters_by_id = {tag.id: tag for tag in masters}
        masters_by_name = {
            self.curation.normalize(tag.tag_name): tag for tag in masters
        }
        projected: list[ProjectedCuratedTag] = []
        suppressed = (
            self._suppression_map([file_id]).get(file_id, set())
            if file_id is not None
            else set()
        )
        for tag in tags:
            if tag.canonical in suppressed:
                continue
            override = overrides.get(tag.canonical)
            if override is not None and override.suppressed:
                continue
            target = (
                masters_by_id.get(override.memorykeeper_tag_id)
                if override is not None and override.memorykeeper_tag_id is not None
                else masters_by_name.get(self.curation.normalize(tag.display_name))
            )
            projected.append(
                ProjectedCuratedTag(
                    identity=f"tag:{target.id}" if target is not None else f"ai:{tag.canonical}",
                    canonical=tag.canonical,
                    display_name=target.tag_name if target is not None else tag.display_name,
                    confidence=tag.confidence,
                    aliases=tuple(
                        dict.fromkeys(
                            (*tag.aliases, tag.display_name, target.tag_name)
                            if target is not None
                            else (*tag.aliases, tag.display_name)
                        )
                    ),
                    curation_version=tag.curation_version,
                    source=(
                        TagSource.USER
                        if override is not None and target is not None
                        else TagSource.AI
                    ),
                    tag_id=target.id if target is not None else None,
                    revision=int(target.revision) if target is not None else 1,
                )
            )
        return projected

    def visible_user_relations(
        self,
        file_id: int,
        relations: list[CommonFileTag],
    ) -> list[CommonFileTag]:
        suppressed = self._suppression_map([file_id]).get(file_id, set())
        if not suppressed:
            return relations
        overrides = self._override_map()
        masters = {tag.id: tag for tag in self._active_tags()}
        result: list[CommonFileTag] = []
        for relation in relations:
            master = masters.get(relation.memorykeeper_tag_id)
            if master is not None:
                canonicals = self._canonical_references_for_tag(master, overrides)
            else:
                rule = self.curation.rule_for(relation.tag)
                canonicals = {rule.canonical} if rule is not None else set()
            if not canonicals.intersection(suppressed):
                result.append(relation)
        return result

    def file_ids_for_query(self, value: str) -> set[int]:
        normalized = self.curation.normalize(value)
        if not normalized:
            return set()
        result: set[int] = set()
        for entry in self._entries().values():
            if normalized in self.curation.normalize(entry.display_name) or any(
                self._query_matches_canonical(normalized, canonical)
                for canonical in entry.canonical_references
            ):
                result.update(entry.file_ids)
        return result

    def _entries(self) -> dict[str, _CatalogEntry]:
        file_rows = (
            self.db.query(CommonFile, CommonFileMetadata)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFile.deleted.is_(False))
            .all()
        )
        file_ids = [common_file.id for common_file, _metadata in file_rows]
        relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id.in_(file_ids))
            .all()
            if file_ids
            else []
        )
        by_file: dict[int, list[CommonFileTag]] = defaultdict(list)
        for relation in relations:
            by_file[relation.file_id].append(relation)

        masters = self._active_tags()
        masters_by_id = {tag.id: tag for tag in masters}
        masters_by_name = {
            self.curation.normalize(tag.tag_name): tag for tag in masters
        }
        overrides = self._override_map()
        suppressed_by_file = self._suppression_map(file_ids)
        entries: dict[str, _CatalogEntry] = {}
        legacy_by_name: dict[str, str] = {}

        for relation in relations:
            if relation.source != TagSource.USER or relation.deleted:
                continue
            normalized = self.curation.normalize(relation.tag)
            master = masters_by_id.get(relation.memorykeeper_tag_id) or masters_by_name.get(
                normalized
            )
            if master is not None:
                identity = f"tag:{master.id}"
                entry = self._entry(entries, identity, master.tag_name, master)
                entry.canonical_references.update(
                    self._canonical_references_for_tag(master, overrides)
                )
            else:
                identity = legacy_by_name.setdefault(normalized, self._legacy_identity(normalized))
                entry = self._entry(entries, identity, relation.tag, None)
                rule = self.curation.rule_for(relation.tag)
                if rule is not None:
                    entry.canonical_references.add(rule.canonical)
            if not entry.canonical_references.intersection(
                suppressed_by_file.get(relation.file_id, set())
            ):
                entry.file_ids.add(relation.file_id)

        for common_file, metadata in file_rows:
            file_relations = by_file.get(common_file.id, [])
            result = self.curation.curate(
                [
                    RawTagInput(relation.tag, relation.confidence)
                    for relation in file_relations
                    if relation.source == TagSource.AI and not relation.deleted
                ],
                user_tags=[
                    relation.tag
                    for relation in file_relations
                    if relation.source == TagSource.USER
                ],
                structured_terms=self._structured_terms(metadata),
            )
            for curated in result.tags:
                override = overrides.get(curated.canonical)
                if override is not None and override.suppressed:
                    continue
                target = (
                    masters_by_id.get(override.memorykeeper_tag_id)
                    if override is not None and override.memorykeeper_tag_id is not None
                    else masters_by_name.get(self.curation.normalize(curated.display_name))
                )
                legacy_identity = legacy_by_name.get(
                    self.curation.normalize(curated.display_name)
                )
                if target is not None:
                    identity = f"tag:{target.id}"
                    entry = self._entry(entries, identity, target.tag_name, target)
                elif legacy_identity is not None:
                    identity = legacy_identity
                    entry = self._entry(entries, identity, curated.display_name, None)
                else:
                    identity = f"ai:{curated.canonical}"
                    entry = self._entry(entries, identity, curated.display_name, None)
                entry.file_ids.add(common_file.id)
                entry.canonical_references.add(curated.canonical)
                if curated.canonical in suppressed_by_file.get(common_file.id, set()):
                    entry.file_ids.discard(common_file.id)

        for master in masters:
            entry = self._entry(entries, f"tag:{master.id}", master.tag_name, master)
            entry.canonical_references.update(
                self._canonical_references_for_tag(master, overrides)
            )
        for canonical, override in overrides.items():
            if override.suppressed or override.memorykeeper_tag_id is None:
                continue
            target = masters_by_id.get(override.memorykeeper_tag_id)
            if target is not None:
                self._entry(
                    entries,
                    f"tag:{target.id}",
                    target.tag_name,
                    target,
                ).canonical_references.add(canonical)
        return entries

    def _ensure_target(self, name: str, active: Tag | None) -> Tag:
        if active is not None:
            return active
        normalized = self.curation.normalize(name)
        existing = self.tags._find_normalized(normalized, include_deleted=True)
        if existing is not None:
            existing.tag_name = name
            existing.normalized_name = normalized
            existing.tag_type = TagType.USER
            existing.source = TagSource.USER
            existing.deleted = False
            existing.revision += 1
            existing.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(existing)
            return existing
        response = self.tags.create(TagCreate(name=name))
        return self.tags.get(response.id)

    def _ensure_canonical_links(
        self,
        canonicals: set[str],
        *,
        tag_id: int | None,
        suppressed: bool,
    ) -> None:
        for canonical in canonicals:
            self._ensure_override(canonical, tag_id=tag_id, suppressed=suppressed)

    def _ensure_override(
        self,
        canonical: str,
        *,
        tag_id: int | None,
        suppressed: bool,
    ) -> MemoryKeeperTagCanonicalOverride:
        normalized = self.curation.normalize(canonical)
        item = (
            self.db.query(MemoryKeeperTagCanonicalOverride)
            .filter(MemoryKeeperTagCanonicalOverride.canonical_key == normalized)
            .first()
        )
        if item is None:
            item = MemoryKeeperTagCanonicalOverride(
                canonical_key=normalized,
                memorykeeper_tag_id=tag_id,
                suppressed=suppressed,
                revision=1,
            )
            self.db.add(item)
            self.db.flush()
            return item
        if item.memorykeeper_tag_id != tag_id or bool(item.suppressed) != suppressed:
            item.memorykeeper_tag_id = tag_id
            item.suppressed = suppressed
            item.revision += 1
            item.updated_at = datetime.now(timezone.utc)
        return item

    def _promote_legacy(self, display_name: str, target: Tag) -> None:
        normalized = self.curation.normalize(display_name)
        for relation in self._legacy_relations(normalized):
            relation.memorykeeper_tag_id = target.id
            relation.tag = target.tag_name
            relation.tag_type = TagType.USER
            relation.source = TagSource.USER
            relation.confidence = None
            relation.deleted = False

    def _legacy_relations(self, normalized: str) -> list[CommonFileTag]:
        return [
            relation
            for relation in (
                self.db.query(CommonFileTag)
                .filter(CommonFileTag.source == TagSource.USER)
                .filter(CommonFileTag.memorykeeper_tag_id.is_(None))
                .filter(CommonFileTag.deleted.is_(False))
                .all()
            )
            if self.curation.normalize(relation.tag) == normalized
        ]

    def _override_map(self) -> dict[str, MemoryKeeperTagCanonicalOverride]:
        return {
            item.canonical_key: item
            for item in self.db.query(MemoryKeeperTagCanonicalOverride).all()
        }

    def _suppression_map(self, file_ids: list[int]) -> dict[int, set[str]]:
        if not file_ids:
            return {}
        rows = (
            self.db.query(MemoryKeeperFileTagSuppression)
            .filter(MemoryKeeperFileTagSuppression.file_id.in_(file_ids))
            .filter(MemoryKeeperFileTagSuppression.deleted.is_(False))
            .all()
        )
        result: dict[int, set[str]] = defaultdict(set)
        for row in rows:
            result[row.file_id].add(row.canonical_key)
        return result

    def _propagate_merged_suppressions(self, canonicals: set[str]) -> None:
        if len(canonicals) < 2:
            return
        active = (
            self.db.query(MemoryKeeperFileTagSuppression)
            .filter(MemoryKeeperFileTagSuppression.canonical_key.in_(canonicals))
            .filter(MemoryKeeperFileTagSuppression.deleted.is_(False))
            .all()
        )
        if not active:
            return
        by_key = {
            (item.file_id, item.canonical_key): item
            for item in (
                self.db.query(MemoryKeeperFileTagSuppression)
                .filter(
                    MemoryKeeperFileTagSuppression.file_id.in_(
                        {item.file_id for item in active}
                    )
                )
                .filter(MemoryKeeperFileTagSuppression.canonical_key.in_(canonicals))
                .all()
            )
        }
        for file_id in {item.file_id for item in active}:
            for canonical in canonicals:
                item = by_key.get((file_id, canonical))
                if item is None:
                    item = MemoryKeeperFileTagSuppression(
                        file_id=file_id,
                        canonical_key=canonical,
                        revision=1,
                        deleted=False,
                    )
                    self.db.add(item)
                    by_key[(file_id, canonical)] = item
                elif item.deleted:
                    item.deleted = False
                    item.revision += 1
                    item.updated_at = datetime.now(timezone.utc)

    def _canonical_references_for_tag(
        self,
        tag: Tag,
        overrides: dict[str, MemoryKeeperTagCanonicalOverride],
    ) -> set[str]:
        canonicals = {
            canonical
            for canonical, override in overrides.items()
            if not override.suppressed and override.memorykeeper_tag_id == tag.id
        }
        rule = self.curation.rule_for(tag.tag_name)
        if rule is not None:
            canonicals.add(rule.canonical)
        return canonicals

    def _active_tags(self) -> list[Tag]:
        return self.db.query(Tag).filter(Tag.deleted.is_(False)).all()

    @staticmethod
    def _entry(
        entries: dict[str, _CatalogEntry],
        identity: str,
        display_name: str,
        master: Tag | None,
    ) -> _CatalogEntry:
        item = entries.get(identity)
        if item is None:
            item = _CatalogEntry(
                identity=identity,
                display_name=display_name,
                favorite=bool(master.favorite) if master is not None else False,
                revision=int(master.revision) if master is not None else 1,
            )
            entries[identity] = item
        return item

    def _require_entry(self, identity: str) -> _CatalogEntry:
        entry = self._entries().get(identity)
        if entry is None:
            self._not_found(identity)
        return entry

    @staticmethod
    def _tag_id(identity: str) -> int | None:
        if not identity.startswith("tag:"):
            return None
        try:
            return int(identity.removeprefix("tag:"))
        except ValueError:
            return None

    @staticmethod
    def _legacy_identity(normalized: str) -> str:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"legacy:{digest}"

    def _query_matches_canonical(self, normalized: str, canonical: str) -> bool:
        return any(
            normalized in self.curation.normalize(term)
            or self.curation.normalize(term) in normalized
            for term in self.curation.search_terms(canonical)
        )

    @staticmethod
    def _structured_terms(metadata: CommonFileMetadata | None) -> list[str]:
        if metadata is None:
            return []
        values = (
            metadata.country,
            metadata.province,
            metadata.city,
            metadata.district,
            metadata.place_name,
        )
        terms = [str(value) for value in values if value]
        if metadata.datetime_original is not None:
            terms.append(str(metadata.datetime_original.year))
        return terms

    @staticmethod
    def _check_revision(identity: str, expected: int, current: int) -> None:
        if expected != current:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REVISION_CONFLICT",
                    "identity": identity,
                    "expected_revision": expected,
                    "current_revision": current,
                },
            )

    @staticmethod
    def _not_found(identity: str) -> None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TAG_IDENTITY_NOT_FOUND", "identity": identity},
        )
