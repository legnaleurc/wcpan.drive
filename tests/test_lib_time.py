"""Tests for time helpers and timestamp handling in lib."""

from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.client.remote._lib import (
    node_record_from_dict,
    utc_from_timestamp,
    utc_now,
)


class _FixedDateTime:
    """Stub bound to wcpan.drive.client.remote.lib.datetime for utc_now tests."""

    UTC = UTC

    @staticmethod
    def now(tz=None):
        return datetime(2022, 6, 15, 12, 30, 0, tzinfo=UTC)


class TestUtcFromTimestamp(TestCase):
    def test_epoch_zero(self):
        # given
        ts = 0
        # when
        dt = utc_from_timestamp(ts)
        # then
        self.assertEqual(dt, datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC))


class TestUtcNow(TestCase):
    def test_returns_patched_fixed_timestamp(self):
        # given
        fixed = int(datetime(2022, 6, 15, 12, 30, 0, tzinfo=UTC).timestamp())
        # when
        with patch("wcpan.drive.protocol.lib.datetime", _FixedDateTime):
            result = utc_now()
        # then
        self.assertEqual(result, fixed)


class TestNodeRecordFromDictTimestamp(TestCase):
    def test_timestamps_are_preserved(self):
        # given
        data = {
            "id": "n1",
            "mutable_id": "n1",
            "parent_id": None,
            "name": "f.txt",
            "is_directory": False,
            "created_time": 1_588_327_200,
            "modified_time": 1_588_417_200,
            "changed_time": 1_588_507_200,
            "mime_type": "text/plain",
            "hash": "",
            "size": 0,
            "is_image": False,
            "is_video": False,
            "width": 0,
            "height": 0,
            "ms_duration": 0,
        }
        # when
        record = node_record_from_dict(data)
        # then
        self.assertEqual(record.created_time, 1_588_327_200)
        self.assertEqual(record.modified_time, 1_588_417_200)
        self.assertEqual(record.changed_time, 1_588_507_200)
