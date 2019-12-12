import contextlib
import tempfile
import unittest

from wcpan.drive.drive import DriveFactory
from wcpan.drive.exceptions import (
    LineageError,
    NodeConflictedError,
    NodeNotFoundError,
    ParentIsNotFolderError,
    RootNodeError,
    TrashedNodeError,
    UploadError,
    DownloadError,
)

from .util import (
    ChangeListBuilder,
    create_file,
    create_folder,
    create_image,
    create_video,
    toggle_trashed,
)


class TestDrive(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        async with contextlib.AsyncExitStack() as stack:
            config_path = stack.enter_context(
                tempfile.TemporaryDirectory()
            )
            data_path = stack.enter_context(
                tempfile.TemporaryDirectory()
            )

            factory = DriveFactory()
            factory.set_config_path(config_path)
            factory.set_data_path(data_path)
            factory.set_database('nodes.db')
            factory.set_driver('tests.driver')
            self._drive = await stack.enter_async_context(
                factory.create_drive()
            )
            self._driver = self._drive._remote

            self._raii = stack.pop_all()

    async def asyncTearDown(self):
        await self._raii.aclose()
        self._driver = None
        self._drive = None
        self._raii = None

    async def testSync(self):
        driver = self._driver
        root_node = await driver.fetch_root_node()

        # normal file and folder
        builder = ChangeListBuilder()
        builder.update(create_folder('id_1', 'name_1', root_node.id_))
        builder.update(create_file(
            'id_2',
            'name_2',
            'id_1',
            2,
            'hash_2',
            'text/plain',
        ))
        driver.set_changes('2', builder.changes)

        applied_changes = [change async for change in self._drive.sync()]
        self.assertEqual(applied_changes, builder.changes)

        node = await self._drive.get_node_by_path('/name_1')
        self.assertTrue(node.is_folder)
        self.assertEqual(node.id_, 'id_1')
        node = await self._drive.get_node_by_path('/name_1/name_2')
        self.assertTrue(node.is_file)
        self.assertEqual(node.id_, 'id_2')
        self.assertEqual(node.size, 2)
        self.assertEqual(node.hash_, 'hash_2')
        self.assertEqual(node.mime_type, 'text/plain')

        # TODO
        # # atomic
        # builder.reset()
        # builder.update(create_file(
        #     'id_3',
        #     'name_3',
        #     'id_1',
        #     3,
        #     'hash_3',
        #     'text/plain',
        # ))
        # driver.set_changes('3', builder.changes)

        # with self.assertRaises(Exception):
        #     async for change in self._drive.sync():
        #         raise Exception('interrupt')

        # node = await self._drive.get_node_by_path('/name_1/name_3')
        # self.assertIsNone(node)

        # image and video
        builder.reset()
        builder.update(create_image(
            'id_4',
            'name_4',
            'id_1',
            4,
            'hash_4',
            'image/png',
            640,
            480,
        ))
        builder.update(create_video(
            'id_5',
            'name_5',
            'id_1',
            5,
            'hash_5',
            'video/mpeg',
            640,
            480,
            5,
        ))
        driver.set_changes('4', builder.changes)

        applied_changes = [change async for change in self._drive.sync()]
        self.assertEqual(applied_changes, builder.changes)

        node = await self._drive.get_node_by_path('/name_1/name_4')
        self.assertTrue(node.is_image)
        self.assertEqual(node.image_width, 640)
        self.assertEqual(node.image_height, 480)
        node = await self._drive.get_node_by_path('/name_1/name_5')
        self.assertTrue(node.is_video)
        self.assertEqual(node.video_width, 640)
        self.assertEqual(node.video_height, 480)
        self.assertEqual(node.video_ms_duration, 5)

        # delete file
        builder.reset()
        builder.delete('id_2')
        driver.set_changes('5', builder.changes)

        applied_changes = [change async for change in self._drive.sync()]
        self.assertEqual(applied_changes, builder.changes)

        node = await self._drive.get_node_by_path('/name_1/name_2')
        self.assertIsNone(node)

        # delete folder
        builder.reset()
        builder.delete('id_1')
        driver.set_changes('5', builder.changes)

        applied_changes = [change async for change in self._drive.sync()]
        self.assertEqual(applied_changes, builder.changes)

        node = await self._drive.get_node_by_path('/name_1')
        self.assertIsNone(node)
        node = await self._drive.get_node_by_id('id_4')
        self.assertIsNone(node.parent_id)
        node = await self._drive.get_node_by_id('id_5')
        self.assertIsNone(node.parent_id)

    async def testCreateFolder(self):
        driver = self._driver
        mock = driver.create_folder_mock
        api = self._drive.create_folder
        root_node = await driver.fetch_root_node()

        builder = ChangeListBuilder()
        builder.update(create_file(
            'id_1',
            'name_1',
            root_node.id_,
            1,
            'hash_1',
            'text/plain',
        ))
        driver.set_changes('2', builder.changes)

        async for dummy_change in self._drive.sync():
            pass

        # invalid parent
        with self.assertRaises(TypeError):
            await api(None, 'name')
        mock.assert_not_called()
        mock.reset_mock()

        # invalid name
        with self.assertRaises(TypeError):
            await api(root_node, None)
        mock.assert_not_called()
        mock.reset_mock()

        # invalid parent
        node = await self._drive.get_node_by_path('/name_1')
        self.assertIsNotNone(node)
        with self.assertRaises(ParentIsNotFolderError):
            await api(node, 'invalid')
        mock.assert_not_called()
        mock.reset_mock()

        # conflict
        with self.assertRaises(NodeConflictedError):
            await api(root_node, 'name_1')
        mock.assert_not_called()
        mock.reset_mock()

        # good calls
        await api(root_node, 'name')
        mock.assert_called_once_with(
            root_node,
            'name',
            None,
            False,
        )
        mock.reset_mock()

        await api(root_node, 'name', True)
        mock.assert_called_once_with(
            root_node,
            'name',
            None,
            True,
        )
        mock.reset_mock()

    async def testRenameNode(self):
        driver = self._driver
        mock = driver.rename_node_mock
        api = self._drive.rename_node
        api_alt = self._drive.rename_node_by_path
        root_node = await driver.fetch_root_node()

        builder = ChangeListBuilder()
        builder.update(create_folder('id_1', 'name_1', root_node.id_))
        builder.update(create_file(
            'id_2',
            'name_2',
            'id_1',
            2,
            'hash_2',
            'text/plain',
        ))
        builder.update(create_folder('id_3', 'name_3', 'id_1'))
        builder.update(create_file(
            'id_4',
            'name_4',
            'id_1',
            4,
            'hash_4',
            'text/plain',
        ))
        builder.update(
            toggle_trashed(
                create_folder(
                    'id_5',
                    'name_5',
                    'id_1',
                ),
            ),
        )
        driver.set_changes('2', builder.changes)

        async for dummy_change in self._drive.sync():
            pass

        # source is not None
        with self.assertRaises(TypeError):
            await api(None, root_node)
        mock.assert_not_called()
        mock.reset_mock()

        # at least have a new parent or new name
        node = await self._drive.get_node_by_path('/name_1')
        with self.assertRaises(TypeError):
            await api(node, None, None)
        mock.assert_not_called()
        mock.reset_mock()

        # do not touch trash can
        node1 = await self._drive.get_node_by_path('/name_1')
        node2 = await self._drive.get_node_by_id('id_5')
        with self.assertRaises(TrashedNodeError):
            await api(node1, node2, None)
        with self.assertRaises(TrashedNodeError):
            await api(node2, node1, None)
        mock.assert_not_called()
        mock.reset_mock()

        # do not move to the source folder
        node = await self._drive.get_node_by_path('/name_1')
        with self.assertRaises(LineageError):
            await api(node, node, None)
        mock.assert_not_called()
        mock.reset_mock()

        # do not move root
        with self.assertRaises(RootNodeError):
            await api_alt('/', '/name_1')
        mock.assert_not_called()
        mock.reset_mock()

        # do not overwrite file
        with self.assertRaises(NodeConflictedError):
            await api_alt('/name_1/name_2', '/name_1/name_4')
        mock.assert_not_called()
        mock.reset_mock()

        # do not move parent to descendant folders
        with self.assertRaises(LineageError):
            await api_alt('/name_1', '/name_1/name_3')
        mock.assert_not_called()
        mock.reset_mock()

        # do not move invalid node
        with self.assertRaises(NodeNotFoundError):
            await api_alt('/invalid', '/name_1')
        mock.assert_not_called()
        mock.reset_mock()

        # do not move to invalid path
        with self.assertRaises(LineageError):
            await api_alt('/name_1/name_2', '/invalid/invalid')
        with self.assertRaises(LineageError):
            await api_alt('/name_1/name_2', './invalid/invalid')
        mock.assert_not_called()
        mock.reset_mock()

        # move to absolute path
        node = await self._drive.get_node_by_path('/name_1')
        await api_alt('/name_1', '/new')
        mock.assert_called_once_with(node, root_node, 'new')
        mock.reset_mock()

        # move to relative folder
        node1 = await self._drive.get_node_by_path('/name_1/name_2')
        node2 = await self._drive.get_node_by_path('/name_1/name_3')
        await api_alt('/name_1/name_2', './name_3')
        mock.assert_called_once_with(node1, node2, None)
        mock.reset_mock()

        # move to relative file
        node1 = await self._drive.get_node_by_path('/name_1/name_2')
        node2 = await self._drive.get_node_by_path('/name_1/name_3')
        await api_alt('/name_1/name_2', './name_3/name_4')
        mock.assert_called_once_with(node1, node2, 'name_4')
        mock.reset_mock()

        # move up
        node = await self._drive.get_node_by_path('/name_1/name_2')
        await api_alt('/name_1/name_2', '..')
        mock.assert_called_once_with(node, root_node, None)
        mock.reset_mock()

    async def testTrashNode(self):
        driver = self._driver
        mock = driver.trash_node_mock
        api = self._drive.trash_node
        root_node = await driver.fetch_root_node()

        builder = ChangeListBuilder()
        builder.update(create_folder('id_1', 'name_1', root_node.id_))
        driver.set_changes('2', builder.changes)

        async for dummy_change in self._drive.sync():
            pass

        # do not trash root node
        with self.assertRaises(RootNodeError):
            await api(root_node)
        mock.assert_not_called()
        mock.reset_mock()

        # do not accept invalid node
        with self.assertRaises(TypeError):
            await api(None)
        mock.assert_not_called()
        mock.reset_mock()

        # good call
        node = await self._drive.get_node_by_path('/name_1')
        await api(node)
        mock.assert_called_once_with(node)
        mock.reset_mock()

    async def testDownload(self):
        driver = self._driver
        mock = driver.download_mock
        api = self._drive.download
        root_node = await driver.fetch_root_node()

        builder = ChangeListBuilder()
        builder.update(create_folder('id_1', 'name_1', root_node.id_))
        builder.update(create_file(
            'id_2',
            'name_2',
            'id_1',
            2,
            'hash_2',
            'text/plain',
        ))
        driver.set_changes('2', builder.changes)

        async for dummy_change in self._drive.sync():
            pass

        # do not download folder
        node = await self._drive.get_node_by_path('/name_1')
        with self.assertRaises(DownloadError):
            await api(node)
        mock.assert_not_called()
        mock.reset_mock()

        # do not accept invalid node
        with self.assertRaises(TypeError):
            await api(None)
        mock.assert_not_called()
        mock.reset_mock()

        # good call
        node = await self._drive.get_node_by_path('/name_1/name_2')
        await api(node)
        mock.assert_called_once_with(node)
        mock.reset_mock()

    async def testUpload(self):
        driver = self._driver
        mock = driver.upload_mock
        api = self._drive.upload
        root_node = await driver.fetch_root_node()

        builder = ChangeListBuilder()
        builder.update(create_folder('id_1', 'name_1', root_node.id_))
        builder.update(create_file(
            'id_2',
            'name_2',
            'id_1',
            2,
            'hash_2',
            'text/plain',
        ))
        driver.set_changes('2', builder.changes)

        async for dummy_change in self._drive.sync():
            pass

        # do not upload to a file
        node = await self._drive.get_node_by_path('/name_1/name_2')
        with self.assertRaises(ParentIsNotFolderError):
            await api(node, 'name_3')
        mock.assert_not_called()
        mock.reset_mock()

        # do not conflict
        node = await self._drive.get_node_by_path('/name_1')
        with self.assertRaises(NodeConflictedError):
            await api(node, 'name_2')
        mock.assert_not_called()
        mock.reset_mock()

        # do not accept invalid node
        with self.assertRaises(TypeError):
            await api(None, 'name_3')
        mock.assert_not_called()
        mock.reset_mock()

        # do not accept invalid name
        with self.assertRaises(TypeError):
            await api(node, None)
        mock.assert_not_called()
        mock.reset_mock()

        # good calls
        await api(node, 'name_3')
        mock.assert_called_once_with(node, 'name_3', None, None, None)
        mock.reset_mock()

        await api(node, 'name_3', 123, 'test/plain')
        mock.assert_called_once_with(node, 'name_3', 123, 'test/plain', None)
        mock.reset_mock()

    async def testGetHasher(self):
        driver = self._driver
        mock = driver.get_hasher_mock

        await self._drive.get_hasher()
        mock.assert_called_once_with()
        mock.reset_mock()
