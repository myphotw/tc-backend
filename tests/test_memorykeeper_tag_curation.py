from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.gallery_repository import GalleryRepository
from app.common.repositories.tag_repository import TagSource, TagType
from app.common.services.gallery_service import GalleryService
from app.memorykeeper.services.tag_curation_service import (
    MemoryKeeperTagCurationService,
    RawTagInput,
)


def raw(name: str, confidence: float = 90) -> RawTagInput:
    return RawTagInput(name=name, confidence=confidence)


class MemoryKeeperTagCurationUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryKeeperTagCurationService()

    def names(self, *items: RawTagInput, user_tags: tuple[str, ...] = ()) -> list[str]:
        return [
            tag.display_name
            for tag in self.service.curate(items, user_tags=user_tags).tags
        ]

    def test_confidence_and_low_value_filters_allow_zero(self) -> None:
        result = self.service.curate((raw("Dog", 69.99), raw("Blue", 99)))
        self.assertEqual(result.tags, ())
        self.assertEqual(
            {item.reason for item in result.rejected},
            {"below_confidence", "low_value"},
        )

    def test_dog_hierarchy_and_duplicate_display_collapse_to_korean(self) -> None:
        result = self.service.curate(
            (raw("Dog", 91), raw("Puppy", 95), raw("Canidae", 99))
        )
        self.assertEqual([tag.display_name for tag in result.tags], ["강아지"])
        self.assertEqual(result.tags[0].canonical, "dog")
        self.assertIn("puppy", result.tags[0].aliases)

    def test_specific_food_wins_and_generic_visual_labels_are_removed(self) -> None:
        self.assertEqual(
            set(self.names(raw("Food"), raw("Ingredient"), raw("Sushi"), raw("Sashimi"))),
            {"초밥", "회"},
        )
        self.assertEqual(self.names(raw("Flooring"), raw("Aluminium")), [])

    def test_frequency_driven_seed_vocabulary_stays_korean(self) -> None:
        result = self.service.curate(
            (
                raw("Seafood"),
                raw("Alcoholic drink"),
                raw("Toy"),
                raw("Wetland"),
            )
        )
        self.assertEqual(
            {tag.display_name for tag in result.tags},
            {"해산물", "술", "장난감", "습지"},
        )
        self.assertEqual(
            set(self.names(raw("Infant"), raw("Car seat"))),
            {"아이", "자동차"},
        )

    def test_category_and_total_limits(self) -> None:
        animal_result = self.service.curate((raw("Dog", 95), raw("Cat", 94)))
        self.assertEqual(len(animal_result.tags), 1)
        self.assertIn(
            "category_limit",
            {item.reason for item in animal_result.rejected},
        )

        result = self.service.curate(
            (
                raw("Dog", 99),
                raw("Sushi", 98),
                raw("Beach", 97),
                raw("Wedding", 96),
                raw("Car", 95),
                raw("Temple", 94),
            )
        )
        self.assertEqual(len(result.tags), 5)
        self.assertIn("max_total", {item.reason for item in result.rejected})

    def test_user_tag_has_semantic_precedence_and_is_not_mutated(self) -> None:
        user_tags = ["강아지"]
        result = self.service.curate((raw("Dog"), raw("Sushi")), user_tags=user_tags)
        self.assertEqual([tag.display_name for tag in result.tags], ["초밥"])
        self.assertEqual(user_tags, ["강아지"])
        self.assertIn("user_precedence", {item.reason for item in result.rejected})

    def test_mapping_is_required_and_search_supports_korean_and_english(self) -> None:
        result = self.service.curate((raw("Useful but unmapped label", 99),))
        self.assertEqual(result.tags, ())
        self.assertEqual(result.rejected[0].reason, "unmapped")
        self.assertIn("dog", self.service.search_terms("강아지"))
        self.assertIn("강아지", self.service.search_terms("dog"))


class MemoryKeeperTagCurationGalleryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def file(self, digest: str, service_name: str) -> CommonFile:
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest[:8]}.jpg",
            service_name=service_name,
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        self.db.add(CommonFileService(file_id=item.id, service_name=service_name))
        return item

    def tag(
        self,
        file: CommonFile,
        name: str,
        *,
        source: str = TagSource.AI,
        confidence: float | None = 95,
    ) -> CommonFileTag:
        item = CommonFileTag(
            file_id=file.id,
            tag=name,
            tag_type=TagType.AI if source == TagSource.AI else TagType.USER,
            source=source,
            confidence=confidence if source == TagSource.AI else None,
            deleted=False,
        )
        self.db.add(item)
        return item

    def test_memorykeeper_projection_is_korean_user_first_and_preserves_raw_rows(self) -> None:
        item = self.file("1" * 64, "MemoryKeeper")
        self.tag(item, "Dog")
        self.tag(item, "Sushi", confidence=93)
        self.tag(item, "강아지", source=TagSource.USER)
        self.db.commit()

        detail = GalleryService(self.db).get_detail(
            item.file_id,
            service_name="MemoryKeeper",
        )

        self.assertEqual([tag.tag for tag in detail.ai_tags], ["초밥"])
        self.assertEqual([tag.tag for tag in detail.user_tags], ["강아지"])
        self.assertEqual([tag.tag for tag in detail.tags], ["강아지", "초밥"])
        self.assertEqual(detail.ai_tags[0].canonical, "sushi")
        self.assertEqual(detail.ai_tags[0].curation_version, 1)
        self.assertEqual(
            {row.tag for row in self.db.query(CommonFileTag).filter_by(file_id=item.id)},
            {"Dog", "Sushi", "강아지"},
        )

    def test_deleted_user_tombstone_still_suppresses_raw_cluster(self) -> None:
        item = self.file("5" * 64, "MemoryKeeper")
        self.tag(item, "Dog")
        decision = self.tag(item, "강아지", source=TagSource.USER)
        decision.deleted = True
        self.db.commit()

        detail = GalleryService(self.db).get_detail(
            item.file_id,
            service_name="MemoryKeeper",
        )

        self.assertEqual(detail.ai_tags, [])
        self.assertEqual(detail.user_tags, [])
        self.assertEqual(detail.tags, [])
        self.assertEqual(
            self.db.query(CommonFileTag).filter_by(file_id=item.id, tag="Dog").count(),
            1,
        )

    def test_astrojournal_raw_projection_is_unchanged(self) -> None:
        item = self.file("2" * 64, "AstroJournal")
        self.tag(item, "Dog")
        self.db.commit()

        detail = GalleryService(self.db).get_detail(
            item.file_id,
            service_name="AstroJournal",
        )
        self.assertEqual([tag.tag for tag in detail.ai_tags], ["Dog"])
        self.assertIsNone(detail.ai_tags[0].canonical)

    def test_memorykeeper_search_expands_korean_and_canonical_only_for_that_service(self) -> None:
        memory_file = self.file("3" * 64, "MemoryKeeper")
        astro_file = self.file("4" * 64, "AstroJournal")
        self.tag(memory_file, "Puppy")
        self.tag(astro_file, "Dog")
        self.db.commit()
        repository = GalleryRepository(self.db)

        korean_rows, korean_total = repository.search(
            tag="강아지",
            service_name="MemoryKeeper",
        )
        english_rows, english_total = repository.search(
            tag="dog",
            service_name="MemoryKeeper",
        )
        astro_rows, astro_total = repository.search(
            tag="강아지",
            service_name="AstroJournal",
        )

        self.assertEqual(korean_total, 1)
        self.assertEqual(korean_rows[0][0].id, memory_file.id)
        self.assertEqual(english_total, 1)
        self.assertEqual(english_rows[0][0].id, memory_file.id)
        self.assertEqual(astro_total, 0)
        self.assertEqual(astro_rows, [])


if __name__ == "__main__":
    unittest.main()
