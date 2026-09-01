"""Configuration-free registry for the complete application model metadata."""

from app.common.model_base import Base

# Keep model registration explicit so migration tooling always sees the same
# complete metadata without importing runtime database settings or an engine.
from app.common.models import api_key as _api_key
from app.common.models import api_usage as _api_usage
from app.common.models import change_event as _change_event
from app.common.models import file as _file
from app.common.models import file_metadata as _file_metadata
from app.common.models import file_service as _file_service
from app.common.models import file_tag as _file_tag
from app.common.models import geocode_cache as _geocode_cache
from app.common.models import metadata_history as _metadata_history
from app.common.models import setting as _setting
from app.common.models import upload_job as _upload_job
from app.common.models import vision_job as _vision_job
from app.common.models import worker_status as _worker_status
from app.memorykeeper.models import file_state as _file_state
from app.memorykeeper.models import file_tag_suppression as _file_tag_suppression
from app.memorykeeper.models import photo as _photo
from app.memorykeeper.models import photo_tag as _photo_tag
from app.memorykeeper.models import place as _place
from app.memorykeeper.models import tag as _tag
from app.memorykeeper.models import tag_canonical_override as _tag_canonical_override
from app.astrojournal.models import observation_record as _observation_record
from app.astrojournal.models import plate_solve_job as _plate_solve_job


__all__ = ["Base"]
