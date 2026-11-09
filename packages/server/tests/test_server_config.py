"""Server config: version 3 layout, legacy version 2 files, and validation."""

import unittest

from wcpan.drive.server.config import (
    CONFIG_VERSION,
    LEGACY_BACKEND_NAME,
    ConfigVersionError,
    parse_config,
)
from wcpan.drive.server.types import MountConfig


def _v3(**overrides):
    config = {
        "version": CONFIG_VERSION,
        "server": {"database_url": "/tmp/mirror.db"},
        "backends": {
            "nas": {
                "type": "wcpan.drive.server.synology.create_backend",
                "url": "https://nas",
            },
            "disk": {"type": "wcpan.drive.server.filesystem.create_backend"},
        },
        "mounts": {
            "photos": {"backend": "nas", "source": "/volume1/photos"},
            "docs": {"backend": "disk", "source": "/srv/docs"},
        },
    }
    config.update(overrides)
    return config


class TestVersion3(unittest.TestCase):
    def test_parses_backends_and_mounts(self):
        # when
        config = parse_config(_v3())
        # then
        self.assertEqual(config.database_url, "/tmp/mirror.db")
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8080))
        self.assertEqual(
            config.backends["nas"].type, "wcpan.drive.server.synology.create_backend"
        )
        self.assertEqual(config.backends["nas"].settings, {"url": "https://nas"})
        self.assertEqual(
            config.mounts["docs"], MountConfig(backend="disk", source="/srv/docs")
        )
        self.assertEqual(config.mounts_of("nas"), {"photos": "/volume1/photos"})

    def test_server_settings(self):
        # when
        config = parse_config(
            _v3(
                server={
                    "database_url": "/tmp/m.db",
                    "host": "0.0.0.0",
                    "port": 9000,
                    "log_path": "/tmp/s.log",
                }
            )
        )
        # then
        self.assertEqual((config.host, config.port), ("0.0.0.0", 9000))
        self.assertEqual(config.log_path, "/tmp/s.log")

    def test_server_upload_tmp_dir_is_rejected(self):
        # given
        raw = _v3(server={"database_url": "/tmp/m.db", "upload_tmp_dir": "/tmp/up"})
        # when / then
        with self.assertRaisesRegex(ValueError, "backends.<name>"):
            parse_config(raw)

    def test_mount_must_name_a_configured_backend(self):
        # given
        raw = _v3()
        raw["mounts"]["x"] = {"backend": "missing", "source": "/x"}
        # when / then
        with self.assertRaisesRegex(ValueError, "unknown backend"):
            parse_config(raw)

    def test_backend_needs_a_type(self):
        # given
        raw = _v3()
        del raw["backends"]["disk"]["type"]
        # when / then
        with self.assertRaisesRegex(ValueError, "backends.disk.type"):
            parse_config(raw)

    def test_mount_names_are_single_components(self):
        # given
        for name in ("", ".", "..", "a/b"):
            raw = _v3()
            raw["mounts"][name] = {"backend": "disk", "source": "/x"}
            # when / then
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_config(raw)


class TestLegacyVersion2(unittest.TestCase):
    def test_reads_as_one_synology_backend(self):
        # when
        config = parse_config(
            {
                "version": 2,
                "database_url": "/tmp/mirror.db",
                "port": 9000,
                "synology": {
                    "url": "https://nas",
                    "webhook": {"callback_url": "x", "app_id": "app"},
                },
                "mounts": {"photos": "/volume1/photos"},
                "local_paths": {"/volume1": "/mnt/nas"},
            }
        )
        # then
        backend = config.backends[LEGACY_BACKEND_NAME]
        self.assertEqual(backend.type, "wcpan.drive.server.synology.create_backend")
        self.assertEqual(backend.settings["url"], "https://nas")
        self.assertNotIn("webhook", backend.settings)
        self.assertEqual(backend.settings["webhook_callback_url"], "x")
        self.assertEqual(backend.settings["webhook_app_id"], "app")
        self.assertEqual(backend.settings["local_paths"], {"/volume1": "/mnt/nas"})
        self.assertEqual(
            config.mounts["photos"],
            MountConfig(backend=LEGACY_BACKEND_NAME, source="/volume1/photos"),
        )
        self.assertEqual(config.port, 9000)

    def test_upload_tmp_dir_moves_to_the_synology_backend(self):
        # when
        config = parse_config(
            {
                "version": 2,
                "database_url": "/tmp/mirror.db",
                "upload_tmp_dir": "/tmp/up",
                "synology": {"url": "https://nas"},
                "mounts": {},
                "local_paths": {},
            }
        )
        # then
        settings = config.backends[LEGACY_BACKEND_NAME].settings
        self.assertEqual(settings["upload_tmp_dir"], "/tmp/up")


class TestVersionCheck(unittest.TestCase):
    def test_missing_version_is_rejected(self):
        # given
        raw = _v3()
        del raw["version"]
        # when / then
        with self.assertRaises(ConfigVersionError):
            parse_config(raw)

    def test_unknown_version_is_rejected(self):
        # when / then
        with self.assertRaises(ConfigVersionError):
            parse_config(_v3(version=99))

    def test_non_integer_version_is_rejected(self):
        # when / then
        with self.assertRaises(ConfigVersionError):
            parse_config(_v3(version="3"))
