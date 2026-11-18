"""Tests for change-scan record conversion."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from decoy import matchers
from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.server.backend.synology._convert import (
    SynologyLocator,
    convert_file_info,
)

from ._lib import DecoyTestCase


def _convert(info, parent_id):
    observation = convert_file_info(info, parent_id)
    assert observation is not None
    return observation.record


class TestConvertSynologyFileInfo(TestCase):
    def test_directory_record(self):
        # given
        info = {
            "file_id": "d1",
            "permanent_link": "d1",
            "parent_id": "p",
            "name": "folder",
            "type": "dir",
            "content_type": "dir",
            "size": 0,
            "created_time": 1700000000,
            "modified_time": 1700003600,
            "change_time": 0,
            "sync_id": 5,
        }
        parent_id = "_mount"
        # when
        observation = convert_file_info(info, parent_id)
        assert observation is not None
        record = observation.record
        # then
        self.assertEqual(observation.locator, "d1")
        self.assertTrue(record.is_directory)
        self.assertEqual(record.mime_type, FOLDER_MIME_TYPE)
        self.assertEqual(record.id, "d1")
        self.assertEqual(record.parent_id, parent_id)
        self.assertEqual(record.created_time, 1700000000)
        self.assertEqual(record.modified_time, 1700003600)

    def test_file_with_image_content_type(self):
        # given
        info = {
            "file_id": "f1",
            "permanent_link": "f1",
            "parent_id": "p",
            "name": "pic.png",
            "type": "file",
            "content_type": "image",
            "hash": "abc",
            "size": 1024,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
        }
        # when
        record = _convert(info, parent_id="par")
        # then
        self.assertFalse(record.is_directory)
        self.assertTrue(record.is_image)
        self.assertFalse(record.is_video)
        self.assertEqual(record.hash, "abc")
        self.assertEqual(record.hash_algorithm, "md4")
        self.assertEqual(record.size, 1024)

    def test_file_with_video_content_type(self):
        # given
        info = {
            "file_id": "v1",
            "permanent_link": "v1",
            "parent_id": "p",
            "name": "clip.mp4",
            "type": "file",
            "content_type": "video",
            "size": 999,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 2,
        }
        # when
        record = _convert(info, parent_id=None)
        # then
        self.assertTrue(record.is_video)
        self.assertIsNone(record.parent_id)

    def test_file_with_image_metadata_does_not_fill_dimensions(self):
        # given
        info = {
            "file_id": "f2",
            "permanent_link": "f2",
            "parent_id": "p",
            "name": "wide.png",
            "type": "file",
            "content_type": "image",
            "size": 10,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
            "image_metadata": {"width": 1920, "height": 1080},
        }
        # when
        record = _convert(info, parent_id="par")
        # then
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)

    def test_file_with_nested_image_metadata_does_not_fill_dimensions(self):
        # given
        info = {
            "file_id": "f3",
            "permanent_link": "f3",
            "parent_id": "p",
            "name": "nested.jpg",
            "type": "file",
            "content_type": "image",
            "size": 10,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
            "image_metadata": {"resolution": {"width": 640, "height": 480}},
        }
        # when
        record = _convert(info, parent_id="par")
        # then
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)

    def test_video_with_image_metadata_does_not_fill_dimensions(self):
        # given
        info = {
            "file_id": "v2",
            "permanent_link": "v2",
            "parent_id": "p",
            "name": "clip.mp4",
            "type": "file",
            "content_type": "video",
            "size": 100,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 2,
            "image_metadata": {
                "width": 1280,
                "height": 720,
                "duration": 5000,
            },
        }
        # when
        record = _convert(info, parent_id="root")
        # then
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)
