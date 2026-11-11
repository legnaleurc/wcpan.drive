"""Bridge between mirror node IDs and Synology Drive API references."""

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.lib.mounts import mount_name
from wcpan.drive.server.lib.names import normalize_name
from wcpan.drive.server.services.operations import LocateUndecidedError
from wcpan.drive.server.types import NodeObservation
from wcpan.synology import (
    SynologyChildRef,
    SynologyClient,
    SynologyError,
    SynologyFileId,
    SynologyFileInfo,
    SynologyNetworkError,
    SynologyParentRef,
    SynologyPath,
    SynologyPermanentLink,
)

from ._convert import SynologyLocator, convert_file_info
from ._mounts import MountRegistry
from ._store import SynologyStore


class SynologyPathService:
    """Bridge between virtual ID space and Synology Drive API.

    Captures *mounts* so callers no longer pass it on every call.
    ``drive_api`` remains a method parameter because it varies per context
    (handlers pull from app keys, services hold their own reference).
    """

    def __init__(
        self,
        *,
        registry: MountRegistry,
        store: SynologyStore,
    ) -> None:
        self._registry = registry
        self._store = store

    @property
    def mounts(self) -> dict[str, SynologyPath]:
        return self._registry.mounts

    async def synology_parent_ref(
        self,
        parent_node_id: NodeId,
    ) -> SynologyParentRef:
        """Translate a mirror parent node ID into a Synology API parent ref."""
        key = mount_name(parent_node_id)
        if key is not None:
            return self._registry.mounts[key]
        return await self._resolve_locator_ref(parent_node_id)

    async def resolve_parent(
        self,
        parent_ref: SynologyFileId,
    ) -> NodeId | None:
        """Mirror node ID for a Synology parent: a stored node or a mount root."""
        parent = await self._store.get_node_by_locator(
            SynologyLocator(parent_ref.file_id)
        )
        if parent is not None:
            return parent.id
        return self._registry.lookup_mount_virtual_id(parent_ref)

    async def locate(
        self,
        drive_api: SynologyClient,
        node_id: NodeId,
    ) -> NodeObservation | None:
        """Fetch *node_id* directly; ``None`` unless it exists under a mirrored parent.

        Only a network failure is undecided. Other API errors count as absence,
        since the error for a missing node is not established.
        """
        try:
            info = await drive_api.get_file(SynologyPermanentLink(node_id))
        except SynologyNetworkError as e:
            raise LocateUndecidedError(str(e)) from e
        except SynologyError:
            return None
        if not info or info.get("removed", False):
            return None
        parent_id = await self.resolve_parent(SynologyFileId(info["parent_id"]))
        if parent_id is None:
            return None
        return convert_file_info(info, parent_id)

    async def list_children(
        self,
        drive_api: SynologyClient,
        parent_node_id: NodeId,
    ) -> list[SynologyFileInfo]:
        """List children of a mirror parent node ID."""
        folder_ref = await self.synology_parent_ref(parent_node_id)
        all_items: list[SynologyFileInfo] = []
        offset = 0
        while True:
            items, total = await drive_api.list_folder(folder_ref, offset=offset)
            all_items.extend(items)
            offset += len(items)
            if offset >= total or not items:
                return all_items

    async def find_child_by_name(
        self,
        drive_api: SynologyClient,
        parent_node_id: NodeId,
        name: str,
    ) -> SynologyFileInfo | None:
        parent_ref = await self.synology_parent_ref(parent_node_id)
        child_ref = SynologyChildRef(
            parent_ref=parent_ref,
            name=normalize_name(name),
        )
        return await drive_api.get_file(child_ref)

    async def _resolve_locator_ref(
        self,
        parent_node_id: NodeId,
    ) -> SynologyFileId:
        locators = await self._store.get_locators([parent_node_id])
        locator = locators.get(parent_node_id)
        if locator is None:
            raise ValueError(
                f"No locator found for non-mount parent_id: {parent_node_id!r}"
            )
        return SynologyFileId(locator)
