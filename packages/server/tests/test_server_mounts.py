"""Mount lookup by name and node ID, and routing by containing mount."""

from typing import cast
from unittest import IsolatedAsyncioTestCase, TestCase

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.services.mirror import MirrorStore
from wcpan.drive.server.services.mounts import Mounts
from wcpan.drive.server.services.operations import (
    NodeOperations,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.routing import BackendRouter


def _dir(node_id: str, parent_id: str | None) -> NodeRecord:
    return NodeRecord(
        id=NodeId(node_id),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
        name=node_id,
        is_directory=True,
        created_time=0,
        modified_time=0,
        changed_time=0,
        mime_type="",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class _Ancestors:
    """``get_ancestors`` over a fixed tree, stopping below mount nodes."""

    def __init__(self, mounts: Mounts, records: list[NodeRecord]) -> None:
        self._mounts = mounts
        self._records = {r.id: r for r in records}
        self.queried: list[NodeId] = []

    async def get_ancestors(self, node_id: NodeId) -> list[NodeRecord]:
        self.queried.append(node_id)
        chain: list[NodeRecord] = []
        current = self._records.get(node_id)
        while current is not None:
            chain.append(current)
            parent = current.parent_id
            if (
                parent is None
                or parent == self._mounts.root_id
                or self._mounts.by_id(parent) is not None
            ):
                break
            current = self._records.get(parent)
        return chain


_ALL = Mounts.from_sources({"docs": "/docs", "pics": "/pics"})
_DOCS = _ALL["docs"]
_PICS = _ALL["pics"]


class TestMounts(TestCase):
    def test_lookup_by_name_and_id(self) -> None:
        # then
        self.assertEqual(list(_ALL), ["docs", "pics"])
        self.assertEqual(_DOCS.source, "/docs")
        self.assertIs(_ALL.by_id(_DOCS.id), _DOCS)
        self.assertIsNone(_ALL.by_id(_ALL.root_id))
        self.assertIsNone(_ALL.by_id(NodeId("docs")))

    def test_subset_serves_only_its_mounts(self) -> None:
        # when
        subset = _ALL.subset(["pics"])
        # then
        self.assertEqual(list(subset), ["pics"])
        self.assertIsNone(subset.by_id(_DOCS.id))
        self.assertIs(subset.by_id(_PICS.id), _PICS)

    def test_chain_ends_under_its_mount(self) -> None:
        # given
        chain = [_dir("file", "folder"), _dir("folder", _DOCS.id)]
        # then
        self.assertIs(_ALL.of_chain(chain), _DOCS)
        self.assertIsNone(_ALL.subset(["pics"]).of_chain(chain))

    def test_empty_or_detached_chain_has_no_mount(self) -> None:
        # then
        self.assertIsNone(_ALL.of_chain([]))
        self.assertIsNone(_ALL.of_chain([_dir("orphan", None)]))
        self.assertIsNone(_ALL.of_chain([_dir("lost", "missing")]))


class TestContaining(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mirror = _Ancestors(
            _ALL,
            [
                _dir(_ALL.root_id, None),
                _dir(_DOCS.id, _ALL.root_id),
                _dir(_PICS.id, _ALL.root_id),
                _dir("folder", _DOCS.id),
                _dir("file", "folder"),
                _dir("photo", _PICS.id),
            ],
        )

    async def _containing(self, mounts: Mounts, node_id: str):
        return await mounts.containing(
            NodeId(node_id), mirror=cast(MirrorStore, self.mirror)
        )

    async def test_mount_node_is_its_own_mount(self) -> None:
        # when
        mount = await self._containing(_ALL, _DOCS.id)
        # then
        self.assertIs(mount, _DOCS)
        self.assertEqual(self.mirror.queried, [])

    async def test_descendant_is_in_its_mount(self) -> None:
        # then
        self.assertIs(await self._containing(_ALL, "file"), _DOCS)
        self.assertIs(await self._containing(_ALL, "photo"), _PICS)

    async def test_nodes_of_other_mounts_are_outside_a_subset(self) -> None:
        # given
        docs_only = _ALL.subset(["docs"])
        # then
        self.assertIsNone(await self._containing(docs_only, "photo"))
        self.assertIsNone(await self._containing(docs_only, _PICS.id))
        self.assertIs(await self._containing(docs_only, "file"), _DOCS)

    async def test_root_and_unknown_nodes_have_no_mount(self) -> None:
        # then
        self.assertIsNone(await self._containing(_ALL, _ALL.root_id))
        self.assertIsNone(await self._containing(_ALL, "unknown"))


class TestBackendOf(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        mirror = _Ancestors(
            _ALL,
            [
                _dir(_DOCS.id, _ALL.root_id),
                _dir("folder", _DOCS.id),
                _dir("photo", _PICS.id),
            ],
        )
        self.router = BackendRouter(
            mirror=cast(MirrorStore, mirror),
            mounts=_ALL.subset(["docs", "pics"]),
            mount_backends={"docs": "disk"},
            operations={"disk": cast(NodeOperations, object())},
            uploaders={},
            files={},
        )

    async def test_node_routes_to_the_backend_of_its_mount(self) -> None:
        # then
        self.assertEqual(await self.router.backend_of(_DOCS.id), "disk")
        self.assertEqual(await self.router.backend_of(NodeId("folder")), "disk")

    async def test_server_root_is_not_supported(self) -> None:
        # when / then
        with self.assertRaises(OperationNotSupportedError):
            await self.router.backend_of(_ALL.root_id)

    async def test_mount_without_backend_is_unavailable(self) -> None:
        # when / then
        with self.assertRaises(OperationUnavailableError):
            await self.router.backend_of(NodeId("photo"))
        with self.assertRaises(OperationUnavailableError):
            await self.router.backend_of(NodeId("unknown"))
