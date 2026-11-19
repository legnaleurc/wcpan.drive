"""Media dimensions probed from the backend's file — applied before each upsert
that emits a change."""

from dataclasses import replace

from wcpan.drive.protocol.types import NodeRecord

from ..._types import NodeObservation, ProbeFailure
from .probe.source import MediaProbeError
from .probing import MediaProbing


class MediaEnrichmentError(RuntimeError):
    def __init__(self, record: NodeRecord, kind: ProbeFailure, reason: str) -> None:
        super().__init__(f"failed to probe {record.id} ({record.name}): {reason}")
        self.record = record
        self.kind = kind
        self.reason = reason


class MediaEnrichService:
    """Probe files for width and height when the record lacks them."""

    def __init__(self, *, probing: MediaProbing) -> None:
        self._probing = probing

    async def enrich(
        self, observation: NodeObservation, *, force_refresh: bool = False
    ) -> NodeRecord:
        """The observed record with probed width and height.

        When ``force_refresh`` is False, skips probing if complete dimensions
        are already present, as a client supplies them. When True, re-probes
        for backfill and scan file batches so stale values can be refreshed.

        Raises:
            MediaEnrichmentError: The probe failed; ``kind`` says why.
        """
        record = observation.record
        if record.is_directory or not (record.is_image or record.is_video):
            return record

        if not force_refresh and record.width > 0 and record.height > 0:
            return record

        try:
            width, height = await self._probing.probe(observation)
        except MediaProbeError as e:
            raise MediaEnrichmentError(record, e.kind, e.reason) from e
        return replace(record, width=width, height=height)
