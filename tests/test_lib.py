"""Tests for wcpan.drive.client.remote.lib.

Each test uses given-when-then sections and unittest assertions.
"""

from unittest import TestCase

from wcpan.drive.client.remote._lib import (
    FOLDER_MIME_TYPE,
    guess_mime_type,
    node_from_record,
    node_record_from_dict,
    node_record_to_dict,
)
from wcpan.drive.protocol.types import MirrorMutableId, MirrorStableId, NodeRecord


_NOW = 1_704_067_200


def _make_record(
    node_id: MirrorStableId = MirrorStableId("file-id-001"),
    name: str = "test.txt",
    parent_id: MirrorStableId | None = MirrorStableId("parent-id-001"),
    is_directory: bool = False,
) -> NodeRecord:
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=_NOW,
        modified_time=_NOW,
        changed_time=_NOW,
        mime_type=FOLDER_MIME_TYPE if is_directory else "text/plain",
        hash="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        mutable_id=MirrorMutableId(str(node_id)),
    )


class TestNodeFromRecord(TestCase):
    def test_id_is_file_id(self):
        # given
        record = _make_record(node_id=MirrorStableId("synology-file-id-xyz"))
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.id, "synology-file-id-xyz")

    def test_parent_id_preserved(self):
        # given
        record = _make_record(parent_id=MirrorStableId("parent-file-id-abc"))
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.parent_id, "parent-file-id-abc")

    def test_root_parent_id_is_none(self):
        # given
        record = _make_record(parent_id=None)
        # when
        node = node_from_record(record)
        # then
        self.assertIsNone(node.parent_id)

    def test_no_private(self):
        # given
        record = _make_record()
        # when
        node = node_from_record(record)
        # then
        self.assertIsNone(node.private)

    def test_name_preserved(self):
        # given
        record = _make_record(name="document.pdf")
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.name, "document.pdf")

    def test_is_directory(self):
        # given
        record = _make_record(is_directory=True)
        # when
        node = node_from_record(record)
        # then
        self.assertTrue(node.is_directory)
        self.assertFalse(node.is_trashed)


class TestGuessMimeType(TestCase):
    def test_directory(self):
        # given
        name = "anything"
        # when
        result = guess_mime_type(name, is_directory=True)
        # then
        self.assertEqual(result, FOLDER_MIME_TYPE)

    def test_known_extension(self):
        # given
        name = "photo.jpg"
        # when
        result = guess_mime_type(name, is_directory=False)
        # then
        self.assertEqual(result, "image/jpeg")

    def test_unknown_extension(self):
        # given
        name = "file.xyz123"
        # when
        result = guess_mime_type(name, is_directory=False)
        # then
        self.assertEqual(result, "application/octet-stream")

    def test_no_extension(self):
        # given
        name = "noextension"
        # when
        result = guess_mime_type(name, is_directory=False)
        # then
        self.assertEqual(result, "application/octet-stream")


class TestNodeRecordRoundtrip(TestCase):
    def test_to_dict_and_back(self):
        # given
        record = _make_record()
        # when
        d = node_record_to_dict(record)
        restored = node_record_from_dict(d)
        # then
        self.assertEqual(restored.id, record.id)
        self.assertEqual(restored.parent_id, record.parent_id)
        self.assertIsInstance(restored.id, str)
        self.assertIsInstance(restored.parent_id, str)
        self.assertEqual(restored.name, record.name)
        self.assertEqual(restored.created_time, record.created_time)
        self.assertEqual(restored.modified_time, record.modified_time)
        self.assertEqual(restored.changed_time, record.changed_time)

    def test_cjk_name(self):
        # given
        record = _make_record(name="照片.jpg")
        # when
        d = node_record_to_dict(record)
        restored = node_record_from_dict(d)
        # then
        self.assertEqual(restored.name, "照片.jpg")
