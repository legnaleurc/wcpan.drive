"""Probing an upload's bytes before the file is stored."""

from dataclasses import replace

from wcpan.drive.protocol.lib import guess_mime_type
from wcpan.drive.protocol.types import MediaInfo

from ..._types import ProbeFailure
from ..media.probe.source import MediaProbeError, MediaSource
from ..media.probing import MediaProbing
from .service import UploadInvalidMediaError, UploadTransientError


async def probe_upload(
    media: MediaProbing, source: MediaSource, kind: MediaInfo
) -> MediaInfo:
    """*kind* with the dimensions probed from an upload's bytes in *source*.

    Probed before the file is stored, so invalid media stores nothing.

    Raises:
        UploadInvalidMediaError: The bytes are not readable media.
        UploadTransientError: The probe failed for another reason; the
            upload can be finished again.
    """
    try:
        width, height = await media.probe_source(source, is_image=kind.is_image)
    except MediaProbeError as e:
        if e.kind == ProbeFailure.INVALID:
            raise UploadInvalidMediaError(f"Cannot read the media: {e.reason}") from e
        raise UploadTransientError(f"Cannot probe the media now: {e.reason}") from e
    return replace(kind, width=width, height=height)


def upload_probe_kind(name: str, media_info: MediaInfo | None) -> MediaInfo | None:
    """What an upload named *name* must be probed as, or ``None``.

    Complete client media info is used as given, as is client media info that
    says the file is not media. Without any, the kind is guessed from the name,
    as the filesystem backend classifies files.
    """
    if media_info is not None:
        if not (media_info.is_image or media_info.is_video):
            return None
        if media_info.width > 0 and media_info.height > 0:
            return None
        return replace(media_info, width=0, height=0)
    mime_type = guess_mime_type(name, is_directory=False)
    if mime_type.startswith("image/"):
        return MediaInfo(is_image=True)
    if mime_type.startswith("video/"):
        return MediaInfo(is_video=True)
    return None
