from dataclasses import replace
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from wcpan.drive.client.exceptions import UnsupportedHashAlgorithmError
from wcpan.drive.client.google._hasher import Md5Hasher, create_hasher
from wcpan.drive.client.google._lib import node_from_api
from wcpan.drive.client.google._service import GoogleDriveBackend


def _api_file(**kwargs: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "f1",
        "name": "a.txt",
        "parents": ["p"],
        "mimeType": "text/plain",
        "trashed": False,
        "createdTime": datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
        "modifiedTime": datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
    }
    data.update(kwargs)
    return data


class NodeFromApiTestCase(IsolatedAsyncioTestCase):
    def testFileWithChecksum(self) -> None:
        node = node_from_api(_api_file(md5Checksum="abc", size="1"))  # type: ignore
        self.assertEqual(node.hash, "abc")
        self.assertEqual(node.hash_algorithm, "md5")

    def testFileWithoutChecksum(self) -> None:
        node = node_from_api(  # type: ignore
            _api_file(mimeType="application/vnd.google-apps.document")
        )
        self.assertEqual(node.hash, "")
        self.assertEqual(node.hash_algorithm, "")


class GetHasherFactoryTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._fs = GoogleDriveBackend(MagicMock(), MagicMock())
        self._node = node_from_api(_api_file(md5Checksum="abc", size="1"))  # type: ignore

    async def testMd5(self) -> None:
        factory = await self._fs.get_hasher_factory(self._node)
        self.assertIs(factory, create_hasher)
        self.assertIsInstance(await factory(), Md5Hasher)

    async def testWithoutChecksum(self) -> None:
        node = replace(self._node, hash="", hash_algorithm="")
        with self.assertRaises(UnsupportedHashAlgorithmError):
            await self._fs.get_hasher_factory(node)

    async def testOtherAlgorithm(self) -> None:
        node = replace(self._node, hash_algorithm="md4")
        with self.assertRaises(UnsupportedHashAlgorithmError):
            await self._fs.get_hasher_factory(node)
