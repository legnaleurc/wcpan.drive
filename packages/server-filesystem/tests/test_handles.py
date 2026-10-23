"""Object identity: stable across renames, shared by hard links."""

import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.server.filesystem._handles import filesystem_id, identify


def _identity(path: Path) -> str:
    return identify(str(path), os.lstat(path), filesystem_id(str(path)))


class TestIdentify(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rename_keeps_identity(self) -> None:
        # given
        path = self.tmp / "a"
        path.write_bytes(b"x")
        before = _identity(path)
        # when
        path.rename(self.tmp / "b")
        # then
        self.assertEqual(_identity(self.tmp / "b"), before)

    def test_hard_links_share_identity(self) -> None:
        # given
        path = self.tmp / "a"
        path.write_bytes(b"x")
        # when
        os.link(path, self.tmp / "b")
        # then
        self.assertEqual(_identity(path), _identity(self.tmp / "b"))

    def test_distinct_files_differ(self) -> None:
        # given
        (self.tmp / "a").write_bytes(b"x")
        (self.tmp / "b").write_bytes(b"x")
        # when / then
        self.assertNotEqual(_identity(self.tmp / "a"), _identity(self.tmp / "b"))

    def test_falls_back_to_inode_without_handle_support(self) -> None:
        # given
        path = self.tmp / "a"
        path.write_bytes(b"x")
        # when
        with patch("wcpan.drive.server.filesystem._handles._handle", return_value=None):
            identity = _identity(path)
        # then
        self.assertTrue(identity.endswith(f":ino:{os.lstat(path).st_ino}"))

    def test_missing_path_raises(self) -> None:
        # when / then
        with self.assertRaises(OSError):
            _identity(self.tmp / "missing")
