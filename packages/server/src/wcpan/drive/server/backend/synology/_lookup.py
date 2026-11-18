"""Drive lookups where a missing node is an answer, not a failure."""

from wcpan.synology.exceptions import (
    SynologyDirectoryNotFoundError,
    SynologyNotFoundError,
)
from wcpan.synology.types import (
    SynologyClient,
    SynologyFileInfo,
    SynologyLookupRef,
)


async def find_file(
    drive_api: SynologyClient, ref: SynologyLookupRef
) -> SynologyFileInfo | None:
    """``get_file``, or ``None`` when Drive reports the node or its folder missing."""
    try:
        return await drive_api.get_file(ref)
    except SynologyNotFoundError:
        return None


async def find_child(
    drive_api: SynologyClient, ref: SynologyLookupRef
) -> SynologyFileInfo | None:
    """``get_file`` of a child by name, or ``None`` when only the child is missing.

    Raises:
        SynologyDirectoryNotFoundError: If Drive reports the parent folder
            missing. Drive may also report a missing folder as a missing node,
            in which case this returns ``None``.
    """
    try:
        return await drive_api.get_file(ref)
    except SynologyDirectoryNotFoundError:
        raise
    except SynologyNotFoundError:
        return None
