from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.client.cli._queue import ProgressTracker


class TestProgressTracker(TestCase):
    def test_no_error(self):
        tracker = ProgressTracker(1)
        with (
            patch("wcpan.drive.client.cli._queue.cout"),
            patch("wcpan.drive.client.cli._queue.cerr"),
        ):
            with tracker.collect("item"):
                pass
        self.assertFalse(tracker.has_error)

    def test_exception_increments_error_and_reraises(self):
        tracker = ProgressTracker(1)
        with (
            patch("wcpan.drive.client.cli._queue.cout"),
            patch("wcpan.drive.client.cli._queue.cerr"),
        ):
            with self.assertRaises(ValueError):
                with tracker.collect("item"):
                    raise ValueError("oops")
        self.assertTrue(tracker.has_error)

    def test_multiple_errors(self):
        tracker = ProgressTracker(2)
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
        self.assertTrue(tracker.has_error)
