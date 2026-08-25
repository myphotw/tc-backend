from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.services.reset_service import AstroJournalResetService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.setting import Setting
from app.common.schema_sync import (
    SERVICE_LINK_BACKFILL_MARKER,
    initialize_database,
    sync_file_service_links,
)
from app.memorykeeper.services.reset_service import MemoryKeeperResetService


class SchemaSyncServiceLinkMigrationTests(unittest.TestCase):
    def _session(self, engine):
        return sessionmaker(bind=engine, expire_on_commit=False)()

    @staticmethod
    def _file(session, marker: str, service_name: str) -> CommonFile:
        item = CommonFile(
            file_id=marker * 64,
            original_name=f"{marker}.jpg",
            service_name=service_name,
        )
        session.add(item)
        session.flush()
        return item

    def test_new_database_startup_records_marker_without_backfill_log(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            self.assertEqual(initialize_database(engine), [])
            session = self._session(engine)
            try:
                marker = session.query(Setting).filter_by(
                    setting_key=SERVICE_LINK_BACKFILL_MARKER
                ).one()
                self.assertEqual(marker.setting_value, "COMPLETED")
                self.assertEqual(session.query(CommonFileService).count(), 0)
            finally:
                session.close()
            self.assertEqual(initialize_database(engine), [])
        finally:
            engine.dispose()

    def test_true_pre_b2_database_backfills_both_services_once(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            CommonFile.__table__.create(engine)
            with engine.begin() as connection:
                connection.execute(
                    CommonFile.__table__.insert(),
                    [
                        {
                            "file_id": "a" * 64,
                            "original_name": "memory.jpg",
                            "service_name": "MemoryKeeper",
                        },
                        {
                            "file_id": "b" * 64,
                            "original_name": "astro.jpg",
                            "service_name": "AstroJournal",
                        },
                    ],
                )

            self.assertIn(
                "backfill:common_file_services=2",
                initialize_database(engine),
            )
            session = self._session(engine)
            try:
                self.assertEqual(
                    {row.service_name for row in session.query(CommonFileService)},
                    {"MemoryKeeper", "AstroJournal"},
                )
                self.assertEqual(
                    session.query(Setting)
                    .filter_by(setting_key=SERVICE_LINK_BACKFILL_MARKER)
                    .count(),
                    1,
                )
            finally:
                session.close()
            self.assertEqual(initialize_database(engine), [])
        finally:
            engine.dispose()

    def test_existing_b2_partial_state_bootstraps_marker_without_repair(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            first = self._file(session, "c", "MemoryKeeper")
            second = self._file(session, "d", "MemoryKeeper")
            astro = self._file(session, "e", "AstroJournal")
            session.add(
                CommonFileService(file_id=first.id, service_name="MemoryKeeper")
            )
            session.commit()

            self.assertEqual(initialize_database(engine), [])
            session.expire_all()
            self.assertEqual(session.query(CommonFileService).count(), 1)
            self.assertIsNone(
                session.query(CommonFileService)
                .filter_by(file_id=second.id, service_name="MemoryKeeper")
                .first()
            )
            self.assertIsNone(
                session.query(CommonFileService)
                .filter_by(file_id=astro.id, service_name="AstroJournal")
                .first()
            )
            self.assertEqual(
                session.query(Setting)
                .filter_by(setting_key=SERVICE_LINK_BACKFILL_MARKER)
                .count(),
                1,
            )
        finally:
            session.close()
            engine.dispose()

    def test_existing_b2_empty_links_bootstraps_marker_without_repair(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            memory = self._file(session, "6", "MemoryKeeper")
            astro = self._file(session, "7", "AstroJournal")
            session.commit()

            self.assertEqual(initialize_database(engine), [])
            session.expire_all()
            self.assertEqual(session.query(CommonFileService).count(), 0)
            self.assertEqual(
                session.query(Setting)
                .filter_by(setting_key=SERVICE_LINK_BACKFILL_MARKER)
                .count(),
                1,
            )
            self.assertEqual(memory.service_name, "MemoryKeeper")
            self.assertEqual(astro.service_name, "AstroJournal")

            self.assertEqual(initialize_database(engine), [])
            self.assertEqual(session.query(CommonFileService).count(), 0)
        finally:
            session.close()
            engine.dispose()

    def test_marker_skips_even_when_explicit_backfill_permission_is_given(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            item = self._file(session, "f", "MemoryKeeper")
            session.add(
                Setting(
                    category="MIGRATION",
                    setting_key=SERVICE_LINK_BACKFILL_MARKER,
                    setting_value="COMPLETED",
                )
            )
            session.commit()

            self.assertEqual(
                sync_file_service_links(engine, allow_legacy_backfill=True),
                [],
            )
            self.assertEqual(
                session.query(CommonFileService).filter_by(file_id=item.id).count(),
                0,
            )
        finally:
            session.close()
            engine.dispose()

    def test_memorykeeper_reset_link_is_not_recreated_on_restart(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            shared = self._file(session, "1", "MemoryKeeper")
            session.add_all(
                [
                    CommonFileService(file_id=shared.id, service_name="MemoryKeeper"),
                    CommonFileService(file_id=shared.id, service_name="AstroJournal"),
                ]
            )
            session.commit()
            initialize_database(engine)

            MemoryKeeperResetService(session).execute()
            self.assertEqual(initialize_database(engine), [])
            session.expire_all()
            links = {
                row.service_name
                for row in session.query(CommonFileService).filter_by(file_id=shared.id)
            }
            self.assertEqual(links, {"AstroJournal"})
            self.assertEqual(shared.service_name, "MemoryKeeper")
        finally:
            session.close()
            engine.dispose()

    def test_astrojournal_reset_link_is_not_recreated_on_restart(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            shared = self._file(session, "2", "AstroJournal")
            session.add_all(
                [
                    CommonFileService(file_id=shared.id, service_name="AstroJournal"),
                    CommonFileService(file_id=shared.id, service_name="MemoryKeeper"),
                    ObservationRecord(
                        file_id=shared.id,
                        service_name="AstroJournal",
                        captured_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                    ),
                ]
            )
            session.commit()
            initialize_database(engine)

            AstroJournalResetService(session).execute()
            self.assertEqual(initialize_database(engine), [])
            session.expire_all()
            links = {
                row.service_name
                for row in session.query(CommonFileService).filter_by(file_id=shared.id)
            }
            self.assertEqual(links, {"MemoryKeeper"})
            self.assertEqual(shared.service_name, "AstroJournal")
        finally:
            session.close()
            engine.dispose()

    def test_manual_delete_after_marker_is_respected_for_all_services(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = self._session(engine)
        try:
            memory = self._file(session, "3", "MemoryKeeper")
            astro = self._file(session, "4", "AstroJournal")
            other = self._file(session, "5", "OtherService")
            session.add_all(
                [
                    CommonFileService(file_id=memory.id, service_name="MemoryKeeper"),
                    CommonFileService(file_id=astro.id, service_name="AstroJournal"),
                    CommonFileService(file_id=other.id, service_name="OtherService"),
                ]
            )
            session.commit()
            initialize_database(engine)
            session.query(CommonFileService).filter(
                CommonFileService.service_name.in_(["MemoryKeeper", "AstroJournal"])
            ).delete(synchronize_session=False)
            session.commit()

            self.assertEqual(initialize_database(engine), [])
            session.expire_all()
            self.assertEqual(
                {row.service_name for row in session.query(CommonFileService)},
                {"OtherService"},
            )
        finally:
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
