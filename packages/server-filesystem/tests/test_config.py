"""Filesystem backend configuration."""

import tempfile
from pathlib import Path
from unittest import TestCase

import yaml
from wcpan.drive.server.config import CONFIG_VERSION
from wcpan.drive.server.filesystem.config import (
    check_mounts,
    load_config,
    parse_filesystem_config,
)


class TestFilesystemConfig(TestCase):
    def test_default_rescan_interval(self) -> None:
        self.assertEqual(parse_filesystem_config({}).rescan_interval, 3600.0)

    def test_rescan_interval_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            parse_filesystem_config({"filesystem": {"rescan_interval": 0}})

    def test_mounts_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            check_mounts({"docs": "relative/docs"})

    def test_nested_mounts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            check_mounts({"a": "/srv/data", "b": "/srv/data/inner/"})

    def test_sibling_prefix_is_not_nested(self) -> None:
        self.assertEqual(
            check_mounts({"a": "/srv/data", "b": "/srv/data2/"}),
            {"a": "/srv/data", "b": "/srv/data2"},
        )

    def test_local_paths_default_to_mount_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "version": CONFIG_VERSION,
                        "database_url": "/tmp/mirror.sqlite",
                        "mounts": {"docs": "/srv/docs/"},
                        "local_paths": {},
                        "filesystem": {"rescan_interval": 60},
                    }
                )
            )
            config, filesystem = load_config(path)
        self.assertEqual(config.mounts, {"docs": "/srv/docs"})
        self.assertEqual(config.local_paths, {"/srv/docs": "/srv/docs"})
        self.assertEqual(filesystem.rescan_interval, 60.0)
