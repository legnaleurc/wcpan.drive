"""Tests for resolve_local_path (no disk / pymediainfo)."""

from pathlib import Path
from unittest import TestCase

from wcpan.drive.protocol.types import NodeRecord
from wcpan.drive.server.services.paths import (
    _resolve_local_path as resolve_local_path,
)


def _node(
    node_id: str,
    name: str,
    parent_id: str | None,
    *,
    is_directory: bool = False,
) -> NodeRecord:
    t = 1_704_067_200
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        created_time=t,
        modified_time=t,
        changed_time=t,
        mime_type="application/octet-stream",
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )


class TestResolveLocalPath(TestCase):
    def test_maps_synology_path_through_volume_prefix(self):
        # given
        file_rec = _node("fid", "readme.txt", "folder_id")
        folder_rec = _node("folder_id", "Projects", "_docs", is_directory=True)
        cache = {"folder_id": folder_rec}
        mounts = {"docs": "/volume1/docs"}
        local_paths = {"/volume1/docs": "/mnt/nas/docs"}
        # when
        path = resolve_local_path(mounts, local_paths, file_rec, cache)
        # then
        self.assertEqual(path, Path("/mnt/nas/docs/Projects/readme.txt"))

    def test_returns_none_when_cache_incomplete(self):
        # given
        file_rec = _node("fid", "a.txt", "missing_parent")
        cache: dict[str, NodeRecord | None] = {}
        mounts = {"docs": "/volume1/docs"}
        local_paths = {"/volume1/docs": "/mnt/x"}
        # when
        path = resolve_local_path(mounts, local_paths, file_rec, cache)
        # then
        self.assertIsNone(path)

    def test_returns_none_when_no_volume_prefix_matches(self):
        # given
        file_rec = _node("fid", "a.txt", "folder_id")
        folder_rec = _node("folder_id", "x", "_docs", is_directory=True)
        cache = {"folder_id": folder_rec}
        mounts = {"docs": "/volume1/docs"}
        local_paths = {"/other/prefix": "/mnt/other"}
        # when
        path = resolve_local_path(mounts, local_paths, file_rec, cache)
        # then
        self.assertIsNone(path)

    def test_picks_longest_local_paths_prefix(self):
        # given
        file_rec = _node("fid", "f.txt", "folder_id")
        folder_rec = _node("folder_id", "sub", "_share", is_directory=True)
        cache = {"folder_id": folder_rec}
        mounts = {"share": "/vol/share"}
        local_paths = {
            "/vol": "/mnt/root",
            "/vol/share": "/mnt/share",
        }
        # when
        path = resolve_local_path(mounts, local_paths, file_rec, cache)
        # then
        self.assertEqual(path, Path("/mnt/share/sub/f.txt"))
