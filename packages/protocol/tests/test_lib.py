"""Tests for the shared wire helpers."""

from unittest import TestCase

from wcpan.drive.protocol.lib import parse_content_range


class TestParseContentRange(TestCase):
    def test_range_with_total(self) -> None:
        self.assertEqual(parse_content_range("bytes 0-99/1000"), (0, 99, 1000))

    def test_single_byte(self) -> None:
        self.assertEqual(parse_content_range("bytes 5-5/6"), (5, 5, 6))

    def test_unknown_total(self) -> None:
        self.assertEqual(parse_content_range("bytes 10-19/*"), (10, 19, None))

    def test_invalid_values(self) -> None:
        for value in (
            "",
            "bytes */1000",  # unsatisfied
            "bytes 9-3/10",  # reversed
            "bytes 0-10/10",  # past the total
            "bytes 0-9",  # no total
            "bytes 0-9/10 ",
            "items 0-9/10",
            "bytes -1-9/10",
            "bytes 0-9/garbage",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_content_range(value))
