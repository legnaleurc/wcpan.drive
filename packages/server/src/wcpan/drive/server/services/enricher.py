"""Media dimensions from local disk — applied before each upsert that emits a change."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from pymediainfo import MediaInfo  # type: ignore[import-untyped]
from wcpan.drive.protocol.types import NodeRecord

from .off_main import OffMainService
from .paths import LocalPathService


class MediaEnrichmentError(RuntimeError):
    def __init__(self, record: NodeRecord, path: Path) -> None:
        super().__init__(f"failed to enrich {record.id} from {path}")
        self.record = record
        self.path = path


def _probe_sync(path: Path, *, is_image: bool) -> tuple[int, int, int] | None:
    """Probe width, height, ms_duration using pymediainfo off the event loop."""
    opts = {"File_TestContinuousFileNames": "0"} if is_image else {}
    info: Any = MediaInfo.parse(str(path), mediainfo_options=opts)

    width = 0
    height = 0
    ms_duration = 0

    for track in info.tracks:
        if track.track_type in ("Video", "Image"):
            width = int(track.width or 0)
            height = int(track.height or 0)
        if track.track_type == "General":
            ms_duration = int(float(track.duration or 0))

    return width, height, ms_duration


class MediaEnrichService:
    """Probe local files for width/height/duration when Synology metadata is missing or stale."""

    def __init__(
        self,
        *,
        local_path_service: LocalPathService,
        off_main: OffMainService | None = None,
    ) -> None:
        self._local_path_service = local_path_service
        self._off_main = off_main

    async def enrich(
        self, record: NodeRecord, *, force_refresh: bool = False
    ) -> NodeRecord:
        """Set width/height/ms_duration via pymediainfo when the file is reachable locally.

        When ``force_refresh`` is False, skips probing if API-like dimensions are already
        present (same as historical ``before_upsert``). When True, re-probes for backfill
        and scan file batches so stale values can be refreshed.
        """
        if record.is_directory or not (record.is_image or record.is_video):
            return record

        if not force_refresh and (
            record.width > 0
            and record.height > 0
            and (not record.is_video or record.ms_duration > 0)
        ):
            return record

        off_main = self._off_main
        if off_main is None:
            raise ValueError("off_main is required for async enrichment")

        local_path = await self._local_path_service.resolve_local_path(record)
        if local_path is None or not local_path.exists():
            return record

        try:
            result = await off_main.untimed(
                _probe_sync, local_path, is_image=record.is_image
            )
        except Exception as e:
            raise MediaEnrichmentError(record, local_path) from e
        if result is None:
            raise MediaEnrichmentError(record, local_path)
        w, h, ms = result
        if w == 0 and h == 0:
            raise MediaEnrichmentError(record, local_path)

        return replace(
            record,
            width=w,
            height=h,
            ms_duration=ms if ms > 0 else record.ms_duration,
        )
