"""Tests for server._services.upload session store."""

import tempfile
from pathlib import Path
from unittest import TestCase

from wcpan.drive.server.services.upload import UploadSessionStore


class TestUploadSessionStore(TestCase):
    def test_create_returns_session_with_correct_fields(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            from wcpan.drive.client.types import MediaInfo

            media = MediaInfo(
                width=100, height=0, ms_duration=0, is_image=True, is_video=False
            )
            session = service.create(
                parent_id="p1",
                name="file.bin",
                total_size=1024,
                mime_type="application/octet-stream",
                media_info=media,
            )
            self.assertEqual(session.parent_id, "p1")
            self.assertEqual(session.name, "file.bin")
            self.assertEqual(session.total_size, 1024)
            self.assertEqual(session.mime_type, "application/octet-stream")
            self.assertEqual(session.media_info, media)
            self.assertEqual(session.received, 0)
            self.assertTrue(session.temp_path.exists())

    def test_create_places_files_in_tmp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            session = service.create("p", "f", 10, None, None)
            self.assertTrue(session.temp_path.is_relative_to(td))

    def test_get_returns_created_session(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            session = service.create("p", "f", 10, None, None)
            result = service.get(session.session_id)
            self.assertIs(result, session)

    def test_get_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            self.assertIsNone(service.get("nonexistent"))

    def test_remove_returns_session_and_makes_it_unreachable(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            session = service.create("p", "f", 10, None, None)
            removed = service.remove(session.session_id)
            self.assertIs(removed, session)
            self.assertIsNone(service.get(session.session_id))

    def test_remove_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            self.assertIsNone(service.remove("nonexistent"))

    def test_close_all_clears_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            s1 = service.create("p", "a", 10, None, None)
            s2 = service.create("p", "b", 20, None, None)
            service.close_all()
            self.assertIsNone(service.get(s1.session_id))
            self.assertIsNone(service.get(s2.session_id))

    def test_session_ids_are_unique(self):
        with tempfile.TemporaryDirectory() as td:
            service = UploadSessionStore(tmp_dir=Path(td))
            sessions = [service.create("p", "f", 10, None, None) for _ in range(10)]
            ids = {s.session_id for s in sessions}
            self.assertEqual(len(ids), 10)
