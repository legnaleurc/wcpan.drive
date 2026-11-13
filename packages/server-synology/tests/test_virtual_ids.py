"""Tests for mount registry and Synology path helpers."""

from pathlib import PurePosixPath
from typing import cast
from unittest import TestCase

from decoy import matchers
from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.backend import Mounts
from wcpan.drive.server.lib.root import SERVER_ROOT_ID
from wcpan.drive.server.synology._convert import SynologyLocator
from wcpan.drive.server.synology._mounts import (
    MountRegistry,
    create_mount_registry,
)
from wcpan.drive.server.synology._paths import SynologyPathService
from wcpan.drive.server.synology._store import SynologyStore
from wcpan.synology import (
    SynologyApiError,
    SynologyChildRef,
    SynologyClient,
    SynologyFileId,
    SynologyFileInfo,
    SynologyPath,
)

from ._lib import DecoyTestCase, IdentityIds, mount_id, mounts_of


def _info(**fields: object) -> SynologyFileInfo:
    return cast(SynologyFileInfo, fields)


class _PathTestCase(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.drive_api = self.decoy.mock(cls=SynologyClient)
        self.store = self.decoy.mock(cls=SynologyStore)
        self.decoy.when(await self.store.get_locators(matchers.Anything())).then_return(
            {}
        )

    def _service(self, mounts: dict[str, SynologyPath] | None = None):
        return SynologyPathService(
            ids=IdentityIds(),
            registry=MountRegistry(mounts=mounts_of(mounts or {}), root_ids={}),
            store=self.store,
        )


class TestSynologyParentRef(_PathTestCase):
    async def test_mount_maps_to_folder_path(self):
        # given
        svc = self._service({"photos": SynologyPath("/volume1/photos")})
        # when
        result = await svc.synology_parent_ref(NodeId("_photos"))
        # then
        self.assertEqual(result, SynologyPath("/volume1/photos"))

    async def test_root_requires_stored_node(self):
        # given
        svc = self._service()
        # when / then
        with self.assertRaises(ValueError):
            await svc.synology_parent_ref(NodeId("_"))

    async def test_real_parent_requires_stored_node(self):
        # given
        svc = self._service()
        # when / then
        with self.assertRaises(ValueError):
            await svc.synology_parent_ref(NodeId("syno-file-7"))


class TestCreateMountRegistry(_PathTestCase):
    async def _stub_roots(self, mounts: dict[str, SynologyPath]) -> None:
        for i, path in enumerate(mounts.values(), start=1):
            self.decoy.when(await self.drive_api.get_file(path)).then_return(
                _info(file_id=f"id-{i}")
            )

    async def _verify_no_lookup(self) -> None:
        self.decoy.verify(await self.drive_api.get_file(matchers.Anything()), times=0)

    async def test_empty_mounts(self):
        # when
        registry = await create_mount_registry(Mounts(), drive_api=self.drive_api)
        # then
        self.assertEqual(registry.paths, {})
        self.assertIsNone(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="unknown"))
        )

    async def test_resolves_root_ids_for_non_nested_mounts(self):
        # given
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "videos": SynologyPath("/volume1/videos"),
        }
        await self._stub_roots(mounts)
        # when
        registry = await create_mount_registry(
            mounts_of(mounts), drive_api=self.drive_api
        )
        # then
        self.assertEqual(registry.paths, mounts)
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-1")),
            "_photos",
        )
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-2")),
            "_videos",
        )

    async def test_nested_mounts_raise_before_metadata_lookup(self):
        # given
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "photos_2024": SynologyPath("/volume1/photos/2024"),
        }
        # when / then
        with self.assertRaises(ValueError):
            await create_mount_registry(mounts_of(mounts), drive_api=self.drive_api)
        # then
        await self._verify_no_lookup()

    async def test_nested_mounts_raise_in_reversed_order(self):
        # given
        mounts = {
            "photos_2024": SynologyPath("/volume1/photos/2024"),
            "photos": SynologyPath("/volume1/photos"),
        }
        # when / then
        with self.assertRaises(ValueError):
            await create_mount_registry(mounts_of(mounts), drive_api=self.drive_api)

    async def test_prefix_but_not_subdirectory_is_ok(self):
        # given
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "photos_archive": SynologyPath("/volume1/photos_archive"),
        }
        await self._stub_roots(mounts)
        # when
        registry = await create_mount_registry(
            mounts_of(mounts), drive_api=self.drive_api
        )
        # then
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-1")),
            "_photos",
        )
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-2")),
            "_photos_archive",
        )

    async def test_trailing_slash_is_normalized_for_nested_check(self):
        # given
        mounts = {
            "a": SynologyPath("/volume1/photos/"),
            "b": SynologyPath("/volume1/photos/2024"),
        }
        # when / then
        with self.assertRaises(ValueError):
            await create_mount_registry(mounts_of(mounts), drive_api=self.drive_api)


class TestFindChildByName(_PathTestCase):
    async def test_mount_parent_calls_get_file_with_child_ref(self):
        # given
        mount_path = SynologyPath("/volume1/photos")
        svc = self._service({"photos": mount_path})
        expected = _info(name="2024")
        self.decoy.when(
            await self.drive_api.get_file(
                SynologyChildRef(parent_ref=mount_path, name="2024")
            )
        ).then_return(expected)
        # when
        result = await svc.find_child_by_name(self.drive_api, NodeId("_photos"), "2024")
        # then
        self.assertEqual(result, expected)

    async def test_non_mount_parent_calls_get_file_with_child_ref(self):
        # given
        self.decoy.when(await self.store.get_locators([NodeId("42")])).then_return(
            {NodeId("42"): SynologyLocator("42")}
        )
        svc = self._service()
        expected = _info(name="img.jpg")
        self.decoy.when(
            await self.drive_api.get_file(
                SynologyChildRef(
                    parent_ref=SynologyFileId(file_id="42"),
                    name="img.jpg",
                )
            )
        ).then_return(expected)
        # when
        result = await svc.find_child_by_name(self.drive_api, NodeId("42"), "img.jpg")
        # then
        self.assertEqual(result, expected)

    async def test_parent_not_found_raises(self):
        # given
        svc = self._service()
        # when / then
        with self.assertRaises(ValueError):
            await svc.find_child_by_name(self.drive_api, NodeId("99"), "child.txt")

    async def test_api_error_propagates(self):
        # given
        mount_path = SynologyPath("/volume1/photos")
        svc = self._service({"photos": mount_path})
        error = SynologyApiError("permission denied", error_code=105)
        self.decoy.when(await self.drive_api.get_file(matchers.Anything())).then_raise(
            error
        )

        # when / then
        with self.assertRaises(SynologyApiError) as caught:
            await svc.find_child_by_name(
                self.drive_api, NodeId("_photos"), "private.txt"
            )

        # then
        self.assertIs(caught.exception, error)
