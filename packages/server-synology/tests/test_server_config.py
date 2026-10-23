"""Tests for server config validation."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from wcpan.drive.server.config import (
    CONFIG_VERSION,
    ConfigVersionError,
    parse_config,
)
from wcpan.drive.server.synology.config import parse_synology_config
from wcpan.drive.server.synology.main import main


def _raw_config(**overrides):
    config = {
        "version": CONFIG_VERSION,
        "database_url": "sqlite:////tmp/test.db",
        "synology": {
            "url": "https://nas.example",
            "username": "user",
            "password": "secret",
            "webhook": {
                "callback_url": "https://public.example",
            },
        },
        "mounts": {"docs": "/docs"},
        "local_paths": {},
    }
    config.update(overrides)
    return config


class TestCheckConfigVersion(unittest.TestCase):
    def test_matching_version_is_accepted(self):
        parse_config(_raw_config())

    def test_missing_version_is_rejected(self):
        raw = _raw_config()
        del raw["version"]
        with self.assertRaises(ConfigVersionError):
            parse_config(raw)

    def test_wrong_version_is_rejected(self):
        with self.assertRaises(ConfigVersionError):
            parse_config(_raw_config(version=99))

    def test_non_integer_version_is_rejected(self):
        with self.assertRaises(ConfigVersionError):
            parse_config(_raw_config(version="2"))


class TestMainConfigVersion(unittest.TestCase):
    def test_gc_rejects_missing_version_before_running_command(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "server.yaml"
            config_path.write_text("database_url: sqlite:////tmp/test.db\n")
            stderr = io.StringIO()
            argv = [
                "wcpan.drive.server",
                "--config",
                str(config_path),
                "gc",
            ]
            with (
                patch("sys.argv", argv),
                patch(
                    "wcpan.drive.server.synology.main.cleanup_dangling_nodes"
                ) as cleanup,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as cm,
            ):
                main()
            self.assertEqual(cm.exception.code, 1)
            cleanup.assert_not_called()
            self.assertIn("config version mismatch", stderr.getvalue())


class TestServerConfigDefaults(unittest.TestCase):
    def test_default_webhook_app_id(self):
        config = parse_synology_config(_raw_config())
        self.assertEqual(config.webhook_app_id, "wcpan-drive-server-synology")

    def test_defaults_host_and_port(self):
        config = parse_config(_raw_config())
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8080)

    def test_maps_nested_config_to_runtime_dataclass(self):
        raw = _raw_config(
            host="0.0.0.0",
            port=9000,
            log_path="/tmp/server.log",
            upload_tmp_dir="/tmp/uploads",
            local_paths={"/docs": "/mnt/docs"},
            synology={
                "url": "https://nas.example:5001",
                "username": "user-2",
                "password": "secret-2",
                "otp_code": "123456",
                "webhook": {
                    "callback_url": "https://callback.example",
                    "app_id": "app-2",
                },
            },
        )
        config = parse_config(raw)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.database_url, "sqlite:////tmp/test.db")
        self.assertEqual(config.mounts, {"docs": "/docs"})
        self.assertEqual(config.local_paths, {"/docs": "/mnt/docs"})
        self.assertEqual(config.log_path, "/tmp/server.log")
        self.assertEqual(config.upload_tmp_dir, "/tmp/uploads")

        synology = parse_synology_config(raw)
        self.assertEqual(synology.url, "https://nas.example:5001")
        self.assertEqual(synology.username, "user-2")
        self.assertEqual(synology.password, "secret-2")
        self.assertEqual(synology.public_url, "https://callback.example")
        self.assertEqual(synology.webhook_app_id, "app-2")
        self.assertEqual(synology.otp_code, "123456")


class TestServerConfigValidation(unittest.TestCase):
    def test_missing_synology_block_is_rejected(self):
        raw = _raw_config()
        del raw["synology"]
        with self.assertRaisesRegex(ValueError, "synology"):
            parse_synology_config(raw)

    def test_missing_webhook_block_is_rejected(self):
        raw = _raw_config()
        del raw["synology"]["webhook"]
        with self.assertRaisesRegex(ValueError, "synology.webhook"):
            parse_synology_config(raw)

    def test_missing_synology_url_is_rejected(self):
        raw = _raw_config()
        del raw["synology"]["url"]
        with self.assertRaisesRegex(ValueError, "synology.url"):
            parse_synology_config(raw)

    def test_missing_callback_url_is_rejected(self):
        raw = _raw_config()
        del raw["synology"]["webhook"]["callback_url"]
        with self.assertRaisesRegex(ValueError, "synology.webhook.callback_url"):
            parse_synology_config(raw)
