from io import StringIO
from pathlib import PurePath
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from tests._lib import aexpect, create_amock
from wcpan.drive.cli._main import amain
from wcpan.drive.core.types import Drive


class TestMain(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        factory = self.enterContext(
            patch("wcpan.drive.cli._main.create_drive_from_config")
        )
        self._drive = create_amock(Drive)
        factory.return_value.__aenter__.return_value = self._drive
        factory.return_value.__aexit__.return_value = None

    @patch("sys.stderr", new_callable=StringIO)
    async def testMkdirNoArgs(self, stderr: StringIO):
        with self.assertRaises(SystemExit):
            await amain(["mkdir"])

    @patch("sys.stderr", new_callable=StringIO)
    async def testMkdirWithPath(self, stderr: StringIO):
        parent_node = MagicMock()
        aexpect(self._drive.get_node_by_path).return_value = parent_node

        rv = await amain(["-cc.yaml", "mkdir", "/var/log"])
        self.assertEqual(rv, 0)

        path = PurePath("/var/log")
        aexpect(self._drive.get_node_by_path).assert_called_once_with(path.parent)
        aexpect(self._drive.create_directory).assert_called_once_with(
            path.name, parent_node, exist_ok=True
        )
