from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.client.cli._queue import ProgressTracker


class TestProgressTracker(TestCase):
    def test_no_error(self):
        # given
        tracker = ProgressTracker(1)
        # when
        with (
            patch("wcpan.drive.client.cli._queue.cout"),
            patch("wcpan.drive.client.cli._queue.cerr"),
        ):
            with tracker.collect("item"):
                pass
        # then
        self.assertFalse(tracker.has_error)

    def test_exception_increments_error_and_reraises(self):
        # given
        tracker = ProgressTracker(1)
        # when
        with (
            patch("wcpan.drive.client.cli._queue.cout"),
            patch("wcpan.drive.client.cli._queue.cerr"),
        ):
            with self.assertRaises(ValueError):
                with tracker.collect("item"):
                    raise ValueError("oops")
        # then
        self.assertTrue(tracker.has_error)

    def test_multiple_errors(self):
        # given
        tracker = ProgressTracker(2)
        # when
        with (
            patch("wcpan.drive.client.cli._queue.cout"),
            patch("wcpan.drive.client.cli._queue.cerr"),
        ):
            with self.assertRaises(ValueError):
                with tracker.collect("item1"):
                    raise ValueError("first")
            with self.assertRaises(RuntimeError):
                with tracker.collect("item2"):
                    raise RuntimeError("second")
        # then
        self.assertTrue(tracker.has_error)
