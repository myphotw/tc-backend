"""Storage Rule Engine."""

from __future__ import annotations

from typing import Any

from app.common.services.storage.storage_rule import (
    AstroJournalStorageRule,
    MemoryKeeperStorageRule,
    StorageRule,
)


class StorageRuleEngine:
    """
    Service Rule을 선택하여 최종 저장 상대경로를 계산한다.

    실제 파일 Move는 수행하지 않는다.
    """

    DEFAULT_SERVICE = "MemoryKeeper"

    def __init__(self, rules: dict[str, StorageRule] | None = None) -> None:
        self.rules: dict[str, StorageRule] = rules or {
            "MemoryKeeper": MemoryKeeperStorageRule(),
            "AstroJournal": AstroJournalStorageRule(),
        }

    def build_path(self, context: Any) -> str:
        """
        선택된 Service Rule로 상대경로를 계산한다.

        Returns:
            str: 예) 2026/대한민국/서울/남산타워
        """
        self._log(context, "RULE_START")
        service_name = (
            getattr(context, "service_name", None) or self.DEFAULT_SERVICE
        )
        self._log(context, f"RULE_SELECTED {service_name}")

        rule = self.rules.get(service_name)
        if rule is None:
            rule = self.rules[self.DEFAULT_SERVICE]
            service_name = self.DEFAULT_SERVICE
            self._log(context, f"RULE_SELECTED {service_name}")

        relative_path = rule.build_path(context).strip("/").replace("\\", "/")
        self._log(context, "RULE_COMPLETE")
        self._log(context, f"FINAL_PATH {relative_path}")
        return relative_path

    @staticmethod
    def _log(context: Any, message: str) -> None:
        log = getattr(context, "log", None)
        if callable(log):
            log(message)
            return
        processing_log = getattr(context, "processing_log", None)
        if isinstance(processing_log, list):
            processing_log.append(message)
