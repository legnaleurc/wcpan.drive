"""Bridge between mirror node IDs and Synology Drive API references.

The native ID of a Synology node is its permanent link; the public ID is minted
by the core. Every conversion of API data goes through ``convert`` so records
leave the backend with public IDs.
"""

from collections.abc import Sequence
from dataclasses import replace

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.exceptions import (
    ListingUnavailableError,
    LocateUndecidedError,
    UnknownNodeError,
)
from wcpan.drive.server.lib import normalize_name
from wcpan.drive.server.types import Mounts, NodeIdMap, NodeObservation
from wcpan.synology.exceptions import (
    SynologyError,
    SynologyNetworkError,
    SynologyNotFoundError,
)
from wcpan.synology.types import (
    SynologyChildRef,
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyLookupRef,
    SynologyParentRef,
    SynologyPath,
    SynologyPermanentLink,
)

from ._convert import SynologyLocator, convert_file_info
from ._lookup import find_child
from ._mounts import MountRegistry
from ._store import SynologyStoreReader


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
        store: SynologyStoreReader,
        ids: NodeIdMap,
    ) -> None:
        self._registry = registry
        self._store = store
        self._ids = ids

    @property
    def ids(self) -> NodeIdMap:
        return self._ids

    async def convert(
        self, items: Sequence[SynologyFileInfo], parent_id: NodeId | None
    ) -> list[tuple[SynologyFileInfo, NodeObservation]]:
        """Observations of *items* under *parent_id*, with public IDs.

        Items without a permanent link are skipped.
        """
        converted = [
            (item, o)
            for item in items
            if (o := convert_file_info(item, parent_id)) is not None
        ]
        ids = await self._ids.public([o.record.id for _, o in converted])
        return [
            (item, replace(o, record=replace(o.record, id=node_id)))
            for (item, o), node_id in zip(converted, ids, strict=True)
        ]

    async def convert_one(
        self, info: SynologyFileInfo, parent_id: NodeId | None
    ) -> NodeObservation | None:
        """``convert`` for one item; ``None`` without a permanent link."""
        converted = await self.convert([info], parent_id)
        return converted[0][1] if converted else None

    async def permanent_link(self, node_id: NodeId) -> SynologyPermanentLink:
        """The stable API reference of *node_id*.

        Raises:
            UnknownNodeError: If *node_id* is not a node of this instance.
        """
        return SynologyPermanentLink(await self._ids.native_of(node_id))

    @property
    def mounts(self) -> Mounts:
        return self._registry.mounts

    @property
    def paths(self) -> dict[str, SynologyPath]:
        """Synology path of each mount, by mount name."""
        return self._registry.paths

    async def synology_parent_ref(
        self,
        parent_node_id: NodeId,
    ) -> SynologyParentRef:
        """Translate a mirror parent node ID into a Synology API parent ref.

        Raises:
            UnknownNodeError: If *parent_node_id* has no locator in the mirror.
        """
        mount = self._registry.mounts.by_id(parent_node_id)
        if mount is not None:
            return self._registry.paths[mount.name]
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

        Only a missing node counts as absence; any other failure is undecided.
        """
        try:
            link = await self.permanent_link(node_id)
        except UnknownNodeError:
            return None
        return await self.locate_link(drive_api, link)

    async def locate_link(
        self,
        drive_api: SynologyClient,
        link: SynologyPermanentLink,
    ) -> NodeObservation | None:
        """``locate`` by permanent link, minting the public ID if it has none."""
        try:
            info = await drive_api.get_file(link)
        except SynologyNotFoundError:
            return None
        except SynologyNetworkError as e:
            raise LocateUndecidedError(str(e)) from e
        except SynologyError as e:
            raise LocateUndecidedError(str(e)) from e
        if info.get("removed", False):
            return None
        parent_id = await self.resolve_parent(SynologyFileId(info["parent_id"]))
        if parent_id is None:
            return None
        return await self.convert_one(info, parent_id)

    async def list_children(
        self,
        drive_api: SynologyClient,
        parent_node_id: NodeId,
    ) -> list[SynologyFileInfo]:
        """List children of a mirror parent node ID.

        A folder is listed by its permanent link, which is stable and known as
        soon as its ID is minted, so a folder can be listed before its own
        write lands.

        Raises:
            UnknownNodeError: If *parent_node_id* is not a node of this instance.
        """
        mount = self._registry.mounts.by_id(parent_node_id)
        folder_ref: SynologyLookupRef = (
            self._registry.paths[mount.name]
            if mount is not None
            else await self.permanent_link(parent_node_id)
        )
        all_items: list[SynologyFileInfo] = []
        offset = 0
        while True:
            items, total = await drive_api.list_folder(folder_ref, offset=offset)
            all_items.extend(items)
            offset += len(items)
            if offset >= total or not items:
                return all_items

    async def list_for_pass(
        self,
        drive_api: SynologyClient,
        parent_node_id: NodeId,
    ) -> list[tuple[SynologyFileInfo, NodeObservation]]:
        """The converted children of a folder, for a scan or comparison pass.

        Items without a permanent link are left out, as by ``convert``.

        Raises:
            ListingUnavailableError: The NAS could not list the folder; the
                pass keeps its subtree. Anything else, such as a reply this
                backend misreads, is a bug and fails it.
        """
        try:
            items = await self.list_children(drive_api, parent_node_id)
        except SynologyError as e:
            raise ListingUnavailableError(str(e)) from e
        return await self.convert(items, parent_node_id)

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
        return await find_child(drive_api, child_ref)

    async def _resolve_locator_ref(
        self,
        parent_node_id: NodeId,
    ) -> SynologyFileId:
        locators = await self._store.get_locators([parent_node_id])
        locator = locators.get(parent_node_id)
        if locator is None:
            # Not mirrored: unknown, or the server root, which has no locator.
            raise UnknownNodeError(parent_node_id)
        return SynologyFileId(locator)
