"""Synology section of the server config and the API client built from it."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dacite import Config, DaciteError, from_dict
from wcpan.drive.server.config import load_raw_config, parse_config
from wcpan.drive.server.types import ServerConfig
from wcpan.synology import SynologyClient, SynologyPath, create_client


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
class _RawSynologySection:
    synology: RawSynologyConfig


@dataclass(frozen=True, kw_only=True)
class SynologyConfig:
    url: str
    username: str
    password: str
    public_url: str
    webhook_app_id: str
    otp_code: str | None = None


def parse_synology_config(raw: Any) -> SynologyConfig:
    try:
        parsed = from_dict(
            data_class=_RawSynologySection, data=raw, config=Config(strict=False)
        ).synology
    except DaciteError as e:
        raise ValueError(str(e)) from e
    return SynologyConfig(
        url=parsed.url,
        username=parsed.username,
        password=parsed.password,
        public_url=parsed.webhook.callback_url,
        webhook_app_id=parsed.webhook.app_id,
        otp_code=parsed.otp_code,
    )


def load_config(config_path: Path) -> tuple[ServerConfig, SynologyConfig]:
    """Parse the generic settings first, so a version mismatch is reported first."""
    raw = load_raw_config(config_path)
    return parse_config(raw), parse_synology_config(raw)


def synology_mounts(config: ServerConfig) -> dict[str, SynologyPath]:
    return {name: SynologyPath(path) for name, path in config.mounts.items()}


def create_synology_client(
    config: SynologyConfig,
) -> AbstractAsyncContextManager[SynologyClient]:
    return create_client(
        base_url=config.url,
        username=config.username,
        password=config.password,
        otp_code=config.otp_code,
    )
