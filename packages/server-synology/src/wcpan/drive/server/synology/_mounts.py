"""Synology mount resolution: configured source paths and their root file IDs."""

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend import Mounts
from wcpan.synology import (
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyPath,
)


def _check_no_nested_mounts(paths: dict[str, SynologyPath]) -> None:
    """Raise if any mount point is a subdirectory of another mount point."""
    sorted_paths = sorted(path.path.rstrip("/") for path in paths.values())
    for i, path_a in enumerate(sorted_paths):
        for path_b in sorted_paths[i + 1 :]:
            if path_b.startswith(path_a + "/"):
                raise ValueError(
                    f"Mount path {path_b!r} is a subdirectory of {path_a!r}; "
                    "nested mount points are not supported"
                )


def synology_paths(mounts: Mounts) -> dict[str, SynologyPath]:
    """Synology path of each mount, by mount name."""
    return {name: SynologyPath(m.source) for name, m in mounts.items()}


class MountRegistry:
    def __init__(
        self,
        *,
        mounts: Mounts,
        root_ids: dict[SynologyFileId, NodeId],
    ) -> None:
        self._mounts = mounts
        self._paths = synology_paths(mounts)
        self._root_ids = root_ids

    @property
    def mounts(self) -> Mounts:
        return self._mounts

    @property
    def paths(self) -> dict[str, SynologyPath]:
        """Synology path of each mount, by mount name."""
        return self._paths

    def lookup_mount_virtual_id(
        self,
        synology_id: SynologyFileId,
    ) -> NodeId | None:
        """Return the mount node ID for a Synology file ID, or None."""
        return self._root_ids.get(synology_id)


async def create_mount_registry(
    mounts: Mounts,
    *,
    drive_api: SynologyClient,
) -> MountRegistry:
    """Resolve Synology root IDs for all mounts. Raises RuntimeError on any failure."""
    paths = synology_paths(mounts)
    _check_no_nested_mounts(paths)
    root_ids: dict[SynologyFileId, NodeId] = {}
    for name, syno_path in paths.items():
        info = await get_file_metadata_by_path(syno_path, drive_api=drive_api)
        if info is None:
            raise RuntimeError(
                f"Mount {name!r} ({syno_path.path}) not found on Synology Drive"
            )
        root_ids[SynologyFileId(file_id=info["file_id"])] = mounts[name].id
    return MountRegistry(mounts=mounts, root_ids=root_ids)


async def get_file_metadata_by_path(
    syno_path: SynologyPath,
    *,
    drive_api: SynologyClient,
) -> SynologyFileInfo | None:
    return await drive_api.get_file(syno_path)
