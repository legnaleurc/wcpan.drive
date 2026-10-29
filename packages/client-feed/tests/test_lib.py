"""Tests for wcpan.drive.client.feed.lib.

Each test uses given-when-then sections and unittest assertions.
"""

from dataclasses import replace
from typing import cast
from unittest import TestCase

from wcpan.drive.client.feed._lib import (
    FOLDER_MIME_TYPE,
    NodeRecordDict,
    guess_mime_type,
    node_from_record,
    node_record_from_dict,
    node_record_to_dict,
)
from wcpan.drive.protocol.types import NodeId, NodeRecord


_NOW = 1_704_067_200


def _make_record(
    node_id: NodeId = NodeId("file-id-001"),
    name: str = "test.txt",
    parent_id: NodeId | None = NodeId("parent-id-001"),
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
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class TestNodeFromRecord(TestCase):
    def test_id_is_file_id(self):
        # given
        record = _make_record(node_id=NodeId("synology-file-id-xyz"))
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.id, "synology-file-id-xyz")

    def test_parent_id_preserved(self):
        # given
        record = _make_record(parent_id=NodeId("parent-file-id-abc"))
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


class TestNodeRecordLocatorCompatibility(TestCase):
    def test_encoder_omits_locator(self):
        d = node_record_to_dict(_make_record())
        self.assertNotIn("mutable_id", d)

    def test_decoder_ignores_legacy_locator(self):
        # Older servers still send the key.
        d: dict[str, object] = dict(node_record_to_dict(_make_record()))
        d["mutable_id"] = "12345"
        self.assertEqual(node_record_from_dict(cast(NodeRecordDict, d)), _make_record())


class TestNodeHashFields(TestCase):
    def test_round_trip_preserves_hash_algorithm(self):
        # given
        record = replace(_make_record(), hash="abc", hash_algorithm="md4")
        # when
        restored = node_record_from_dict(node_record_to_dict(record))
        node = node_from_record(restored)
        # then
        self.assertEqual(node.hash, "abc")
        self.assertEqual(node.hash_algorithm, "md4")

    def test_pending_file_has_neither_field(self):
        # given
        record = _make_record()
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.hash, "")
        self.assertEqual(node.hash_algorithm, "")

    def test_hash_without_algorithm_is_rejected(self):
        # given
        record = replace(_make_record(), hash="abc")
        # then
        with self.assertRaises(ValueError):
            node_from_record(record)

    def test_algorithm_without_hash_is_rejected(self):
        # given
        record = replace(_make_record(), hash_algorithm="md4")
        # then
        with self.assertRaises(ValueError):
            node_from_record(record)

    def test_unknown_algorithm_is_passed_through(self):
        # given
        record = replace(_make_record(), hash="abc", hash_algorithm="crc32")
        # when
        node = node_from_record(record)
        # then
        self.assertEqual(node.hash_algorithm, "crc32")

    def test_directory_with_hash_is_rejected(self):
        # given
        record = replace(
            _make_record(is_directory=True), hash="abc", hash_algorithm="md4"
        )
        # then
        with self.assertRaises(ValueError):
            node_from_record(record)
