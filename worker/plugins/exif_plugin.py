from __future__ import annotations

from app.common.repositories.metadata_repository import (
    MetadataRepository,
    MetadataSource,
)
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.services.photo_analysis import ExifReader
from app.memorykeeper.services.capture_date_service import (
    MemoryKeeperCaptureDateService,
)
from worker.plugins.base import BasePlugin, PluginContext


class ExifPlugin(BasePlugin):
    """
    Storage 완료 후 원본 이미지 EXIF를 추출하여 Metadata Platform에 저장한다.
    """

    plugin_name = "ExifPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 50
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if context.common_file is None:
            raise ValueError("common_file is required before EXIF extraction")

        source_path = context.original_path or context.storage_path
        if source_path is None:
            raise ValueError("original_path is required before EXIF extraction")

        # MetadataPlugin이 flush만 한 변경분, immutable EXIF fact, and the
        # MemoryKeeper projection are committed as one explicit unit.
        try:
            exif_metadata = ExifReader().read(source_path)
            context.metadata.update(exif_metadata)

            gps_lat = exif_metadata.get("gps_lat")
            gps_lon = exif_metadata.get("gps_lon")
            context.gps_lat = gps_lat
            context.gps_lon = gps_lon
            context.has_gps = gps_lat is not None and gps_lon is not None

            metadata_repository = MetadataRepository(context.db)
            metadata = metadata_repository.upsert_fields(
                file_id=context.common_file.id,
                values=exif_metadata,
                source=MetadataSource.EXIF,
                modified_by="ExifPlugin",
                commit=False,
            )
            metadata_repository.set_original_capture_datetime_if_missing(
                item=metadata,
                value=exif_metadata.get("datetime_original"),
                modified_by="ExifPlugin",
                commit=False,
            )

            service_name = context.service_name or "MemoryKeeper"
            if service_name.casefold() == "memorykeeper":
                link = context.file_service_link or FileServiceRepository(
                    context.db
                ).get(
                    file_id=context.common_file.id,
                    service_name="MemoryKeeper",
                )
                if link is not None:
                    state = MemoryKeeperCaptureDateService(context.db).synchronize(
                        common_file=context.common_file,
                        service_link=link,
                        metadata=metadata,
                        state=context.memorykeeper_state,
                    )
                    context.file_service_link = link
                    context.memorykeeper_state = state
            context.db.commit()
        except Exception:
            context.db.rollback()
            raise
        context.log("EXIF_COMPLETE")
