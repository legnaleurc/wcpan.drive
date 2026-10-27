"""Server config loading and validation.

Version 3 names backend instances and the mounts they serve::

    version: 3
    server:
      host: "0.0.0.0"
      port: 8080
      database_url: "/data/mirror.db"
    backends:
      nas:
        type: wcpan.drive.server.synology.create_backend
        ...
    mounts:
      photos:
        backend: nas
        source: "/volume1/photos"

Each backend section is passed to the backend factory named by ``type``. Version 2
files, which configured one Synology server, are read as a single backend
instance named ``synology``, with its nested ``webhook`` section flattened.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from dacite import Config, DaciteError, MissingValueError, WrongTypeError, from_dict

from .types import BackendConfig, MountConfig, ServerConfig


CONFIG_VERSION = 3
LEGACY_CONFIG_VERSION = 2
LEGACY_BACKEND_NAME = "synology"
_LEGACY_BACKEND_TYPE = "wcpan.drive.server.synology.create_backend"


class ConfigVersionError(ValueError):
    """Raised when the YAML config schema version is missing or unsupported."""


@dataclass(frozen=True, slots=True)
class RawServerSection:
    database_url: str
    host: str = "127.0.0.1"
    port: int = 8080
    log_path: str | None = None
    upload_tmp_dir: str | None = None


@dataclass(frozen=True, slots=True)
class RawMountConfig:
    backend: str
    source: str


@dataclass(frozen=True, slots=True)
class RawServerConfig:
    version: int
    server: RawServerSection
    backends: dict[str, dict[str, Any]]
    mounts: dict[str, RawMountConfig]


@dataclass(frozen=True, slots=True)
class _RawLegacyConfig:
    database_url: str
    mounts: dict[str, str]
    local_paths: dict[str, str]
    synology: dict[str, Any]
    host: str = "127.0.0.1"
    port: int = 8080
    log_path: str | None = None
    upload_tmp_dir: str | None = None


@dataclass(frozen=True, slots=True)
class _RawVersion:
    version: int


def load_config(config_path: Path) -> ServerConfig:
    with open(config_path) as f:
        raw: Any = yaml.safe_load(f)
    return parse_config(raw)


def parse_config(raw: Any) -> ServerConfig:
    version = _parse_version(raw)
    try:
        if version == LEGACY_CONFIG_VERSION:
            config = _from_legacy(_parse(_RawLegacyConfig, raw))
        else:
            config = _from_raw(_parse(RawServerConfig, raw))
    except DaciteError as e:
        raise ValueError(str(e)) from e
    _validate(config)
    return config


def _parse[T](data_class: type[T], raw: Any) -> T:
    return from_dict(data_class=data_class, data=raw, config=Config(strict=False))


def _parse_version(raw: Any) -> int:
    expected = f"{CONFIG_VERSION} (or legacy {LEGACY_CONFIG_VERSION})"
    try:
        version = _parse(_RawVersion, raw).version
    except MissingValueError as e:
        raise ConfigVersionError(
            f"config version mismatch: expected {expected}, got missing"
        ) from e
    except WrongTypeError as e:
        raise ConfigVersionError(
            f"config version mismatch: expected {expected}, got {e.value!r}"
        ) from e
    except DaciteError as e:
        raise ValueError(str(e)) from e
    if version not in (CONFIG_VERSION, LEGACY_CONFIG_VERSION):
        raise ConfigVersionError(
            f"config version mismatch: expected {expected}, got {version}"
        )
    return version


def _from_raw(raw: RawServerConfig) -> ServerConfig:
    backends: dict[str, BackendConfig] = {}
    for name, section in raw.backends.items():
        settings = dict(section)
        backend_type = settings.pop("type", None)
        if not isinstance(backend_type, str) or not backend_type:
            raise ValueError(f"backends.{name}.type must name a backend factory")
        backends[name] = BackendConfig(type=backend_type, settings=settings)
    return ServerConfig(
        host=raw.server.host,
        port=raw.server.port,
        database_url=raw.server.database_url,
        backends=backends,
        mounts={
            name: MountConfig(backend=m.backend, source=m.source)
            for name, m in raw.mounts.items()
        },
        log_path=raw.server.log_path,
        upload_tmp_dir=raw.server.upload_tmp_dir,
    )


def _from_legacy(raw: _RawLegacyConfig) -> ServerConfig:
    settings = dict(raw.synology)
    webhook: object = settings.pop("webhook", None)
    if isinstance(webhook, dict):
        for key, value in cast(dict[str, Any], webhook).items():
            settings[f"webhook_{key}"] = value
    settings["local_paths"] = dict(raw.local_paths)
    return ServerConfig(
        host=raw.host,
        port=raw.port,
        database_url=raw.database_url,
        backends={
            LEGACY_BACKEND_NAME: BackendConfig(
                type=_LEGACY_BACKEND_TYPE, settings=settings
            )
        },
        mounts={
            name: MountConfig(backend=LEGACY_BACKEND_NAME, source=source)
            for name, source in raw.mounts.items()
        },
        log_path=raw.log_path,
        upload_tmp_dir=raw.upload_tmp_dir,
    )


def _validate(config: ServerConfig) -> None:
    for name in config.backends:
        if not name:
            raise ValueError("Backend names must not be empty")
    for name, mount in config.mounts.items():
        if not name or name in (".", "..") or "/" in name:
            raise ValueError(f"Invalid mount name {name!r}")
        if mount.backend not in config.backends:
            raise ValueError(f"Mount {name!r} uses unknown backend {mount.backend!r}")


__all__ = [
    "CONFIG_VERSION",
    "ConfigVersionError",
    "LEGACY_BACKEND_NAME",
    "LEGACY_CONFIG_VERSION",
    "load_config",
    "parse_config",
]
