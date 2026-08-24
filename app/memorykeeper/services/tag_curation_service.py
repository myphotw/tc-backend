"""MemoryKeeper user-facing automatic tag curation.

The Google Vision rows in ``common_file_tags`` are the immutable-ish raw input
for this projection.  This module deliberately has no database dependency so a
new curation version can be previewed and re-applied without calling Vision or
rewriting the raw labels.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Iterable


@dataclass(frozen=True)
class RawTagInput:
    name: str
    confidence: float | None


@dataclass(frozen=True)
class CuratedTag:
    canonical: str
    display_name: str
    category: str
    confidence: float
    aliases: tuple[str, ...]
    curation_version: int


@dataclass(frozen=True)
class RejectedTag:
    name: str
    confidence: float | None
    reason: str


@dataclass(frozen=True)
class CurationResult:
    tags: tuple[CuratedTag, ...]
    rejected: tuple[RejectedTag, ...]


@dataclass(frozen=True)
class _VocabularyRule:
    canonical: str
    display_name: str
    category: str
    aliases: tuple[str, ...]
    priority: int = 50
    suppresses: tuple[str, ...] = ()


_RULES = (
    _VocabularyRule("person", "사람", "person", ("person", "people", "human", "face"), 20),
    _VocabularyRule(
        "child",
        "아이",
        "person",
        ("child", "children", "kid", "toddler", "infant", "baby", "baby in car seat"),
        80,
        ("person",),
    ),
    _VocabularyRule("selfie", "셀카", "person", ("selfie", "self portrait"), 90, ("person",)),
    _VocabularyRule("group", "단체", "person", ("group", "group of people", "crowd"), 70, ("person",)),
    _VocabularyRule("walking", "걷기", "activity", ("walking", "walk")),
    _VocabularyRule("driving", "운전", "activity", ("driving", "driver")),
    _VocabularyRule("cooking", "요리", "activity", ("cooking", "cook")),
    _VocabularyRule("swimming", "수영", "activity", ("swimming", "swimmer"), 70),
    _VocabularyRule("hiking", "등산", "activity", ("hiking", "trekking"), 70),
    _VocabularyRule("sushi", "초밥", "food", ("sushi",), 90),
    _VocabularyRule("sashimi", "회", "food", ("sashimi",), 90),
    _VocabularyRule("pizza", "피자", "food", ("pizza",), 80),
    _VocabularyRule("coffee", "커피", "food", ("coffee", "cafe", "espresso"), 70),
    _VocabularyRule("dessert", "디저트", "food", ("dessert", "cake", "pastry"), 60),
    _VocabularyRule("seafood", "해산물", "food", ("seafood", "fish slice", "해산물"), 75),
    _VocabularyRule("alcohol", "술", "food", ("alcoholic drink", "liquor", "alcohol", "drinking", "술"), 70),
    _VocabularyRule("salad", "샐러드", "food", ("salad", "샐러드"), 70),
    _VocabularyRule("brunch", "브런치", "food", ("brunch", "브런치"), 70),
    _VocabularyRule(
        "dog",
        "강아지",
        "animal",
        (
            "dog",
            "puppy",
            "canidae",
            "canid",
            "carnivore",
            "carnivores",
            "working animal",
            "german spitz klein",
            "강아지",
            "개",
        ),
        90,
    ),
    _VocabularyRule("cat", "고양이", "animal", ("cat", "kitten", "felidae", "고양이"), 90),
    _VocabularyRule("bird", "새", "animal", ("bird", "birds", "avian", "새"), 60),
    _VocabularyRule("fish", "물고기", "animal", ("fish", "물고기"), 50),
    _VocabularyRule("mountain", "산", "nature", ("mountain", "mountains", "산"), 70),
    _VocabularyRule("sea", "바다", "nature", ("sea", "ocean", "바다"), 60),
    _VocabularyRule("beach", "해변", "nature", ("beach", "shore", "seashore", "해변"), 80),
    _VocabularyRule("waterfall", "폭포", "nature", ("waterfall", "폭포"), 90),
    _VocabularyRule("forest", "숲", "nature", ("forest", "woodland", "숲"), 70),
    _VocabularyRule("flower", "꽃", "nature", ("flower", "flowers", "blossom", "꽃"), 60),
    _VocabularyRule("snow", "눈", "nature", ("snow", "snowfall", "눈"), 60),
    _VocabularyRule("wedding", "결혼식", "event", ("wedding", "wedding ceremony", "결혼식"), 90),
    _VocabularyRule("fireworks", "불꽃놀이", "event", ("fireworks", "firework", "불꽃놀이"), 90),
    _VocabularyRule("festival", "축제", "event", ("festival", "축제"), 70),
    _VocabularyRule(
        "car",
        "자동차",
        "transport",
        ("car", "automobile", "vehicle", "car seat", "car door", "seat belt", "자동차"),
        60,
    ),
    _VocabularyRule("train", "기차", "transport", ("train", "railway", "locomotive", "기차"), 70),
    _VocabularyRule("airplane", "비행기", "transport", ("airplane", "aeroplane", "aircraft", "비행기"), 70),
    _VocabularyRule("boat", "배", "transport", ("boat", "ship", "watercraft", "배"), 60),
    _VocabularyRule("fishing", "낚시", "hobby", ("fishing", "angler", "낚시"), 80),
    _VocabularyRule("camping", "캠핑", "hobby", ("camping", "campsite", "캠핑"), 80),
    _VocabularyRule("skiing", "스키", "hobby", ("skiing", "ski", "스키"), 80),
    _VocabularyRule("cycling", "자전거", "hobby", ("cycling", "bicycle", "bike", "자전거"), 70),
    _VocabularyRule("play", "놀이", "hobby", ("play", "recreation", "놀이"), 60),
    _VocabularyRule("toy", "장난감", "hobby", ("toy", "baby toys", "toy dog", "장난감"), 70),
    _VocabularyRule("figurine", "피규어", "hobby", ("figurine", "action figure", "collectable", "피규어"), 75),
    _VocabularyRule("temple", "사찰", "place", ("temple", "buddhist temple", "사찰"), 80),
    _VocabularyRule("pagoda", "탑", "place", ("pagoda", "stupa", "탑"), 70),
    _VocabularyRule("museum", "박물관", "place", ("museum", "박물관"), 80),
    _VocabularyRule("castle", "성", "place", ("castle", "fortress", "성"), 70),
    _VocabularyRule("amusement_park", "놀이공원", "place", ("amusement park", "theme park", "놀이공원"), 90),
    _VocabularyRule("wetland", "습지", "nature", ("wetland", "bayou", "습지"), 70),
)


class MemoryKeeperTagCurationService:
    """Create a small Korean, search-oriented projection from raw Vision labels."""

    CURATION_VERSION = 1
    CONFIDENCE_THRESHOLD = 70.0
    MAX_TAGS = 5
    DEFAULT_CATEGORY_LIMIT = 1
    CATEGORY_LIMITS = {"person": 2, "food": 2, "nature": 2}

    # Small exceptions only. Unknown labels are handled by the positive
    # vocabulary, rather than growing an unbounded blacklist.
    LOW_VALUE_LABELS = frozenset(
        {
            "blue",
            "yellow",
            "color",
            "flooring",
            "aluminium",
            "aluminum",
            "transparency",
            "lens flare",
            "backlighting",
            "comfort",
            "material",
            "wood",
            "furniture",
            "tableware",
            "plate",
            "food",
            "ingredient",
            "surface",
        }
    )

    def __init__(self) -> None:
        self._rules_by_alias = {
            self.normalize(alias): rule
            for rule in _RULES
            for alias in (rule.canonical, rule.display_name, *rule.aliases)
        }

    def curate(
        self,
        raw_tags: Iterable[RawTagInput],
        *,
        user_tags: Iterable[str] = (),
        structured_terms: Iterable[str] = (),
    ) -> CurationResult:
        rejected: list[RejectedTag] = []
        candidates: dict[str, tuple[_VocabularyRule, RawTagInput]] = {}

        for raw in raw_tags:
            normalized = self.normalize(raw.name)
            confidence = float(raw.confidence) if raw.confidence is not None else 0.0
            if confidence < self.CONFIDENCE_THRESHOLD:
                rejected.append(RejectedTag(raw.name, raw.confidence, "below_confidence"))
                continue
            if normalized in self.LOW_VALUE_LABELS:
                rejected.append(RejectedTag(raw.name, raw.confidence, "low_value"))
                continue
            rule = self._rules_by_alias.get(normalized)
            if rule is None:
                rejected.append(RejectedTag(raw.name, raw.confidence, "unmapped"))
                continue
            previous = candidates.get(rule.canonical)
            if previous is None or confidence > float(previous[1].confidence or 0):
                if previous is not None:
                    rejected.append(
                        RejectedTag(previous[1].name, previous[1].confidence, "duplicate_cluster")
                    )
                candidates[rule.canonical] = (rule, raw)
            else:
                rejected.append(RejectedTag(raw.name, raw.confidence, "duplicate_cluster"))

        user_canonicals = {
            rule.canonical
            for value in user_tags
            if (rule := self.rule_for(value)) is not None
        }
        structured = {self.normalize(value) for value in structured_terms if value}
        suppressed = {
            canonical
            for rule, _raw in candidates.values()
            for canonical in rule.suppresses
        }

        eligible: list[tuple[_VocabularyRule, RawTagInput]] = []
        for canonical, (rule, raw) in candidates.items():
            if canonical in user_canonicals:
                rejected.append(RejectedTag(raw.name, raw.confidence, "user_precedence"))
                continue
            if canonical in suppressed:
                rejected.append(RejectedTag(raw.name, raw.confidence, "more_specific_tag"))
                continue
            searchable_names = {
                self.normalize(rule.canonical),
                self.normalize(rule.display_name),
                *(self.normalize(alias) for alias in rule.aliases),
            }
            if structured.intersection(searchable_names):
                rejected.append(RejectedTag(raw.name, raw.confidence, "structured_metadata"))
                continue
            eligible.append((rule, raw))

        eligible.sort(
            key=lambda item: (item[0].priority, float(item[1].confidence or 0)),
            reverse=True,
        )
        selected: list[CuratedTag] = []
        category_counts: dict[str, int] = {}
        displays: set[str] = set()
        for rule, raw in eligible:
            display_key = self.normalize(rule.display_name)
            if display_key in displays:
                rejected.append(RejectedTag(raw.name, raw.confidence, "duplicate_display"))
                continue
            category_limit = self.CATEGORY_LIMITS.get(
                rule.category,
                self.DEFAULT_CATEGORY_LIMIT,
            )
            if category_counts.get(rule.category, 0) >= category_limit:
                rejected.append(RejectedTag(raw.name, raw.confidence, "category_limit"))
                continue
            if len(selected) >= self.MAX_TAGS:
                rejected.append(RejectedTag(raw.name, raw.confidence, "max_total"))
                continue
            selected.append(
                CuratedTag(
                    canonical=rule.canonical,
                    display_name=rule.display_name,
                    category=rule.category,
                    confidence=float(raw.confidence or 0),
                    aliases=rule.aliases,
                    curation_version=self.CURATION_VERSION,
                )
            )
            displays.add(display_key)
            category_counts[rule.category] = category_counts.get(rule.category, 0) + 1

        return CurationResult(tags=tuple(selected), rejected=tuple(rejected))

    def search_terms(self, value: str) -> tuple[str, ...]:
        """Expand Korean/English canonical and aliases for DB raw-tag search."""
        normalized = self.normalize(value)
        if not normalized:
            return ()
        matching_rules = {
            rule
            for alias, rule in self._rules_by_alias.items()
            if normalized in alias or alias in normalized
        }
        if not matching_rules:
            return (value.strip(),)
        terms = {value.strip()}
        for rule in matching_rules:
            terms.update((rule.canonical, rule.display_name, *rule.aliases))
        return tuple(
            sorted(
                (term for term in terms if term),
                key=lambda item: (len(item), item),
            )
        )

    def rule_for(self, value: str) -> _VocabularyRule | None:
        return self._rules_by_alias.get(self.normalize(value))

    @staticmethod
    def normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
        return re.sub(r"\s+", " ", normalized)
