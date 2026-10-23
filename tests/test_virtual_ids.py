"""Tests for virtual node_id grammar and mount registry helpers."""

from pathlib import PurePosixPath
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.types import MirrorStableId
from wcpan.drive.server.lib.mounts import (
    SERVER_ROOT_ID,
    MountRegistry,
    create_mount_registry,
    is_mount_node_id,
    is_virtual,
    mount_id,
    mount_name,
)
from wcpan.drive.server.services.paths import SynologyPathService
from wcpan.drive.server.types import SynologyLocator
from wcpan.synology import (
    SynologyApiError,
    SynologyChildRef,
    SynologyFileId,
    SynologyPath,
)


class TestIsVirtual(TestCase):
    def test_root_is_virtual(self):
        # given
        node_id = SERVER_ROOT_ID
        # when
        result = is_virtual(node_id)
        # then
        self.assertTrue(result)

    def test_mount_is_virtual(self):
        # given
        node_id = MirrorStableId("_photos")
        # when
        result = is_virtual(node_id)
        # then
        self.assertTrue(result)

    def test_synology_id_not_virtual(self):
        # given
        node_id = MirrorStableId("12345")
        # when
        result = is_virtual(node_id)
        # then
        self.assertFalse(result)


class TestIsMountNodeId(TestCase):
    def test_bare_root_not_mount(self):
        # given
        node_id = MirrorStableId("_")
        # when
        result = is_mount_node_id(node_id)
        # then
        self.assertFalse(result)

    def test_named_mount_is_mount(self):
        # given
        node_id = MirrorStableId("_share-a")
        # when
        result = is_mount_node_id(node_id)
        # then
        self.assertTrue(result)


class TestMountId(TestCase):
    def test_prefixes_name(self):
        # given
        name = "my-share"
        # when
        result = mount_id(name)
        # then
        self.assertEqual(result, "_my-share")


class TestMountName(TestCase):
    def test_returns_key_for_mount(self):
        # given
        node_id = MirrorStableId("_foo")
        # when
        result = mount_name(node_id)
        # then
        self.assertEqual(result, "foo")

    def test_root_returns_none(self):
        # given
        node_id = MirrorStableId("_")
        # when
        result = mount_name(node_id)
        # then
        self.assertIsNone(result)

    def test_real_id_returns_none(self):
        # given
        node_id = MirrorStableId("999")
        # when
        result = mount_name(node_id)
        # then
        self.assertIsNone(result)


class TestSynologyParentRef(IsolatedAsyncioTestCase):
    async def test_mount_maps_to_folder_path(self):
        # given
        svc = SynologyPathService(
            registry=MountRegistry(
                mounts={"photos": "/volume1/photos"},
                root_ids={},
            ),
            storage=MagicMock(),
        )
        # when
        result = await svc.synology_parent_ref(MirrorStableId("_photos"))
        # then
        self.assertEqual(result, "/volume1/photos")

    async def test_root_requires_stored_node(self):
        # given
        storage = MagicMock()
        storage.get_locators = AsyncMock(return_value={})
        svc = SynologyPathService(
            registry=MountRegistry(mounts={}, root_ids={}),
            storage=storage,
        )
        # when / then
        with self.assertRaises(ValueError):
            await svc.synology_parent_ref(MirrorStableId("_"))

    async def test_real_parent_requires_stored_node(self):
        # given
        storage = MagicMock()
        storage.get_locators = AsyncMock(return_value={})
        svc = SynologyPathService(
            registry=MountRegistry(mounts={}, root_ids={}),
            storage=storage,
        )
        # when / then
        with self.assertRaises(ValueError):
            await svc.synology_parent_ref(MirrorStableId("syno-file-7"))


class TestCreateMountRegistry(IsolatedAsyncioTestCase):
    async def test_empty_mounts(self):
        registry = await create_mount_registry({}, drive_api=MagicMock())
        self.assertEqual(registry.mounts, {})
        self.assertIsNone(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="unknown"))
        )

    async def test_resolves_root_ids_for_non_nested_mounts(self):
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "videos": SynologyPath("/volume1/videos"),
        }
        drive_api = MagicMock()
        with patch(
            "wcpan.drive.server.lib.mounts.get_file_metadata_by_path",
            new_callable=AsyncMock,
            side_effect=[
                {"file_id": "id-1"},
                {"file_id": "id-2"},
            ],
        ) as mock_get:
            registry = await create_mount_registry(mounts, drive_api=drive_api)
        self.assertEqual(registry.mounts, mounts)
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-1")),
            "_photos",
        )
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-2")),
            "_videos",
        )
        self.assertEqual(mock_get.await_count, 2)

    async def test_nested_mounts_raise_before_metadata_lookup(self):
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "photos_2024": SynologyPath("/volume1/photos/2024"),
        }
        with patch(
            "wcpan.drive.server.lib.mounts.get_file_metadata_by_path",
            new_callable=AsyncMock,
        ) as mock_get:
            with self.assertRaises(ValueError):
                await create_mount_registry(mounts, drive_api=MagicMock())
        mock_get.assert_not_awaited()

    async def test_nested_mounts_raise_in_reversed_order(self):
        mounts = {
            "photos_2024": SynologyPath("/volume1/photos/2024"),
            "photos": SynologyPath("/volume1/photos"),
        }
        with self.assertRaises(ValueError):
            await create_mount_registry(mounts, drive_api=MagicMock())

    async def test_prefix_but_not_subdirectory_is_ok(self):
        mounts = {
            "photos": SynologyPath("/volume1/photos"),
            "photos_archive": SynologyPath("/volume1/photos_archive"),
        }
        with patch(
            "wcpan.drive.server.lib.mounts.get_file_metadata_by_path",
            new_callable=AsyncMock,
            side_effect=[
                {"file_id": "id-1"},
                {"file_id": "id-2"},
            ],
        ):
            registry = await create_mount_registry(mounts, drive_api=MagicMock())
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-1")),
            "_photos",
        )
        self.assertEqual(
            registry.lookup_mount_virtual_id(SynologyFileId(file_id="id-2")),
            "_photos_archive",
        )

    async def test_trailing_slash_is_normalized_for_nested_check(self):
        mounts = {
            "a": SynologyPath("/volume1/photos/"),
            "b": SynologyPath("/volume1/photos/2024"),
        }
        with self.assertRaises(ValueError):
            await create_mount_registry(mounts, drive_api=MagicMock())


class TestFindChildByName(IsolatedAsyncioTestCase):
    async def test_mount_parent_calls_get_file_with_child_ref(self):
        # given
        mount_path = SynologyPath("/volume1/photos")
        svc = SynologyPathService(
            registry=MountRegistry(mounts={"photos": mount_path}, root_ids={}),
            storage=MagicMock(),
        )
        drive_api = MagicMock()
        expected = {"name": "2024"}
        drive_api.get_file = AsyncMock(return_value=expected)
        # when
        result = await svc.find_child_by_name(
            drive_api, MirrorStableId("_photos"), "2024"
        )
        # then
        drive_api.get_file.assert_awaited_once_with(
            SynologyChildRef(parent_ref=mount_path, name="2024")
        )
        self.assertEqual(result, expected)

    async def test_non_mount_parent_calls_get_file_with_child_ref(self):
        # given
        storage = MagicMock()
        storage.get_locators = AsyncMock(return_value={"42": SynologyLocator("42")})
        svc = SynologyPathService(
            registry=MountRegistry(mounts={}, root_ids={}),
            storage=storage,
        )
        drive_api = MagicMock()
        expected = {"name": "img.jpg"}
        drive_api.get_file = AsyncMock(return_value=expected)
        # when
        result = await svc.find_child_by_name(
            drive_api, MirrorStableId("42"), "img.jpg"
        )
        # then
        drive_api.get_file.assert_awaited_once_with(
            SynologyChildRef(
                parent_ref=SynologyFileId(file_id="42"),
                name="img.jpg",
            )
        )
        self.assertEqual(result, expected)

    async def test_parent_not_found_raises(self):
        # given
        storage = MagicMock()
        storage.get_locators = AsyncMock(return_value={})
        svc = SynologyPathService(
            registry=MountRegistry(mounts={}, root_ids={}),
            storage=storage,
        )
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(return_value=None)
        # when / then
        with self.assertRaises(ValueError):
            await svc.find_child_by_name(drive_api, MirrorStableId("99"), "child.txt")

    async def test_api_error_propagates(self):
        mount_path = SynologyPath("/volume1/photos")
        svc = SynologyPathService(
            registry=MountRegistry(mounts={"photos": mount_path}, root_ids={}),
            storage=MagicMock(),
        )
        error = SynologyApiError("permission denied", error_code=105)
        drive_api = MagicMock()
        drive_api.get_file = AsyncMock(side_effect=error)

        with self.assertRaises(SynologyApiError) as caught:
            await svc.find_child_by_name(
                drive_api, MirrorStableId("_photos"), "private.txt"
            )

        self.assertIs(caught.exception, error)
