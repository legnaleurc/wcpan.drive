"""Server config loading and validation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dacite import Config, DaciteError, MissingValueError, WrongTypeError, from_dict
from wcpan.synology import SynologyPath

from .types import ServerConfig


CONFIG_VERSION = 2


class ConfigVersionError(ValueError):
    """Raised when the YAML config schema version is missing or unsupported."""


@dataclass(frozen=True, slots=True)
class RawWebhookConfig:
    callback_url: str
    app_id: str = "wcpan-drive-server-synology"


@dataclass(frozen=True, slots=True)
class RawSynologyConfig:
    url: str
    username: str
    password: str
    webhook: RawWebhookConfig
    otp_code: str | None = None


@dataclass(frozen=True, slots=True)
class RawServerConfig:
    version: int
    database_url: str
    mounts: dict[str, str]
    synology: RawSynologyConfig
    local_paths: dict[str, str]
    host: str = "127.0.0.1"
    port: int = 8080
    log_path: str | None = None
    upload_tmp_dir: str | None = None


def load_config(config_path: Path) -> ServerConfig:
    with open(config_path) as f:
        raw: Any = yaml.safe_load(f)
    return parse_config(raw)


def parse_config(raw: Any) -> ServerConfig:
    try:
        parsed = _parse_raw_config(raw)
    except DaciteError as e:
        raise ValueError(str(e)) from e
    _check_config_version(parsed)
    return _server_config_from_raw(parsed)


def _parse_raw_config(raw: Any) -> RawServerConfig:
    try:
        return from_dict(
            data_class=RawServerConfig,
            data=raw,
            config=Config(strict=False),
        )
    except MissingValueError as e:
        if e.field_path == "version":
            raise ConfigVersionError(
                f"config version mismatch: expected {CONFIG_VERSION}, got missing"
            ) from e
        raise
    except WrongTypeError as e:
        if e.field_path == "version":
            raise ConfigVersionError(
                f"config version mismatch: expected {CONFIG_VERSION}, got {e.value!r}"
            ) from e
        raise


def _check_config_version(raw: RawServerConfig) -> None:
    version = raw.version
    if version != CONFIG_VERSION:
        raise ConfigVersionError(
            f"config version mismatch: expected {CONFIG_VERSION}, got {version}"
        )


def _server_config_from_raw(raw: RawServerConfig) -> ServerConfig:
    return ServerConfig(
        host=raw.host,
        port=raw.port,
        database_url=raw.database_url,
        synology_url=raw.synology.url,
        username=raw.synology.username,
        password=raw.synology.password,
        mounts={k: SynologyPath(v) for k, v in raw.mounts.items()},
        public_url=raw.synology.webhook.callback_url,
        webhook_app_id=raw.synology.webhook.app_id,
        local_paths=raw.local_paths,
        otp_code=raw.synology.otp_code,
        log_path=raw.log_path,
        upload_tmp_dir=raw.upload_tmp_dir,
    )


__all__ = [
    "CONFIG_VERSION",
    "ConfigVersionError",
    "RawServerConfig",
    "load_config",
    "parse_config",
]
