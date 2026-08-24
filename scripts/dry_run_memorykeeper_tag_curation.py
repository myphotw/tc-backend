"""Preview MemoryKeeper tag curation against existing raw rows without writes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from statistics import mean, median

from sqlalchemy import or_, text

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.tag_repository import TagSource
from app.memorykeeper.services.tag_curation_service import (
    MemoryKeeperTagCurationService,
    RawTagInput,
)
from app.memorykeeper.models.tag import Tag


def build_report(*, top: int = 20) -> dict[str, object]:
    db = SessionLocal()
    try:
        # This is intentionally the first statement in the transaction.  The
        # script never calls schema initialization, flush, commit, or a worker.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))

        rows = (
            db.query(CommonFileTag)
            .join(CommonFile, CommonFile.id == CommonFileTag.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == "MemoryKeeper")
            .filter(
                or_(
                    CommonFileTag.source == TagSource.USER,
                    CommonFileTag.deleted.is_(False),
                )
            )
            .order_by(CommonFileTag.file_id.asc(), CommonFileTag.id.asc())
            .all()
        )
        by_file: dict[int, list[CommonFileTag]] = defaultdict(list)
        for row in rows:
            by_file[row.file_id].append(row)

        service = MemoryKeeperTagCurationService()
        raw_counts: list[int] = []
        curated_counts: list[int] = []
        distribution = Counter({index: 0 for index in range(service.MAX_TAGS + 1)})
        rejected_labels: Counter[str] = Counter()
        unmapped_labels: Counter[str] = Counter()
        kept_labels: Counter[str] = Counter()
        rejection_reasons: Counter[str] = Counter()
        curated_by_file: dict[int, object] = {}
        canonical_raw_labels: dict[str, Counter[str]] = defaultdict(Counter)

        for tags in by_file.values():
            raw_ai = [tag for tag in tags if tag.source == TagSource.AI]
            if not raw_ai:
                continue
            user = [tag.tag for tag in tags if tag.source == TagSource.USER]
            result = service.curate(
                [RawTagInput(name=tag.tag, confidence=tag.confidence) for tag in raw_ai],
                user_tags=user,
            )
            curated_by_file[tags[0].file_id] = result
            raw_counts.append(len(raw_ai))
            curated_counts.append(len(result.tags))
            distribution[len(result.tags)] += 1
            kept_labels.update(tag.display_name for tag in result.tags)
            for item in result.rejected:
                rejection_reasons[item.reason] += 1
                rejected_labels[item.name] += 1
                if item.reason == "unmapped":
                    unmapped_labels[item.name] += 1
            for tag in raw_ai:
                if float(tag.confidence or 0) < service.CONFIDENCE_THRESHOLD:
                    continue
                rule = service.rule_for(tag.tag)
                if rule is not None:
                    canonical_raw_labels[rule.canonical][tag.tag] += 1

        active_masters = db.query(Tag).filter(Tag.deleted.is_(False)).all()
        masters_by_id = {tag.id: tag for tag in active_masters}
        masters_by_name = {
            service.normalize(tag.tag_name): tag for tag in active_masters
        }
        catalog: dict[str, dict[str, object]] = {}
        legacy_by_name: dict[str, str] = {}
        for tags in by_file.values():
            for relation in tags:
                if relation.source != TagSource.USER or relation.deleted:
                    continue
                normalized = service.normalize(relation.tag)
                master = masters_by_id.get(
                    relation.memorykeeper_tag_id
                ) or masters_by_name.get(normalized)
                if master is not None:
                    identity = f"tag:{master.id}"
                    display = master.tag_name
                else:
                    identity = legacy_by_name.setdefault(
                        normalized,
                        "legacy:"
                        + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
                    )
                    display = relation.tag
                entry = catalog.setdefault(
                    identity,
                    {"display_name": display, "file_ids": set(), "canonicals": set()},
                )
                entry["file_ids"].add(relation.file_id)

        for master in active_masters:
            catalog.setdefault(
                f"tag:{master.id}",
                {"display_name": master.tag_name, "file_ids": set(), "canonicals": set()},
            )
        for file_id, result in curated_by_file.items():
            for tag in result.tags:
                normalized = service.normalize(tag.display_name)
                master = masters_by_name.get(normalized)
                if master is not None:
                    identity = f"tag:{master.id}"
                    display = master.tag_name
                elif normalized in legacy_by_name:
                    identity = legacy_by_name[normalized]
                    display = tag.display_name
                else:
                    identity = f"ai:{tag.canonical}"
                    display = tag.display_name
                entry = catalog.setdefault(
                    identity,
                    {"display_name": display, "file_ids": set(), "canonicals": set()},
                )
                entry["file_ids"].add(file_id)
                entry["canonicals"].add(tag.canonical)

        display_counts = Counter(
            service.normalize(str(entry["display_name"]))
            for entry in catalog.values()
        )
        catalog_top = sorted(
            (
                {
                    "identity": identity,
                    "display_name": entry["display_name"],
                    "usage_count": len(entry["file_ids"]),
                    "canonical_references": sorted(entry["canonicals"]),
                }
                for identity, entry in catalog.items()
            ),
            key=lambda item: (-item["usage_count"], str(item["display_name"])),
        )

        raw_total = sum(raw_counts)
        curated_total = sum(curated_counts)
        target_count = len(raw_counts)
        orphan_count = int(
            db.execute(
                text(
                    """
                    SELECT count(*)
                    FROM common_file_tags AS relation
                    LEFT JOIN mk_tags AS tag
                      ON tag.id = relation.memorykeeper_tag_id
                    WHERE relation.memorykeeper_tag_id IS NOT NULL
                      AND tag.id IS NULL
                    """
                )
            ).scalar_one()
        )
        foreign_keys: list[dict[str, object]] = []
        if db.get_bind().dialect.name == "postgresql":
            foreign_keys = [
                {"name": name, "validated": bool(validated)}
                for name, validated in db.execute(
                    text(
                        """
                        SELECT conname, convalidated
                        FROM pg_constraint
                        WHERE conrelid = 'common_file_tags'::regclass
                          AND contype = 'f'
                        ORDER BY conname
                        """
                    )
                ).all()
            ]
        return {
            "mode": "READ_ONLY_DRY_RUN",
            "service_name": "MemoryKeeper",
            "curation_version": service.CURATION_VERSION,
            "confidence_threshold": service.CONFIDENCE_THRESHOLD,
            "target_file_count": target_count,
            "existing_raw_tag_count": raw_total,
            "existing_average_tag_count": round(mean(raw_counts), 2) if raw_counts else 0,
            "existing_median_tag_count": float(median(raw_counts)) if raw_counts else 0,
            "curated_tag_count": curated_total,
            "curated_average_tag_count": round(mean(curated_counts), 2) if curated_counts else 0,
            "curated_count_distribution": {
                str(index): distribution[index]
                for index in range(service.MAX_TAGS + 1)
            },
            "zero_tag_count": distribution[0],
            "zero_tag_ratio": round(distribution[0] / target_count, 4) if target_count else 0,
            "removal_ratio": round(1 - (curated_total / raw_total), 4) if raw_total else 0,
            "rejection_reasons": dict(rejection_reasons.most_common()),
            "top_unmapped_candidates": unmapped_labels.most_common(top),
            "top_removed_raw_labels": rejected_labels.most_common(top),
            "top_curated_korean_tags": kept_labels.most_common(top),
            "unified_catalog": {
                "unique_tag_count": len(catalog),
                "duplicate_display_count": sum(
                    count - 1 for count in display_counts.values() if count > 1
                ),
                "top_usage": catalog_top[:top],
                "canonical_cluster_raw_labels": [
                    {
                        "canonical": canonical,
                        "raw_labels": labels.most_common(),
                    }
                    for canonical, labels in sorted(
                        canonical_raw_labels.items(),
                        key=lambda item: (-sum(item[1].values()), item[0]),
                    )[:top]
                ],
            },
            "memorykeeper_tag_fk": {
                "orphan_count": orphan_count,
                "foreign_keys": foreign_keys,
            },
        }
    finally:
        db.rollback()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only MemoryKeeper raw Vision tag curation preview",
    )
    parser.add_argument("--top", type=int, default=20, help="top label count")
    args = parser.parse_args()
    print(json.dumps(build_report(top=max(1, args.top)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
