"""Synology backend settings and the API client built from them."""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from dacite import Config, DaciteError, from_dict
from wcpan.synology import SynologyClient, create_client


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
    local_paths: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, kw_only=True)
class SynologyConfig:
    url: str
    username: str
    password: str
    public_url: str
    webhook_app_id: str
    otp_code: str | None = None
    # Synology path prefix to host path, for local media probing.
    local_paths: dict[str, str] = field(default_factory=dict[str, str])


def parse_synology_config(settings: Mapping[str, Any]) -> SynologyConfig:
    """Parse one Synology backend section (without its ``type``)."""
    try:
        parsed = from_dict(
            data_class=RawSynologyConfig,
            data=dict(settings),
            config=Config(strict=False),
        )
    except DaciteError as e:
        raise ValueError(str(e)) from e
    return SynologyConfig(
        url=parsed.url,
        username=parsed.username,
        password=parsed.password,
        public_url=parsed.webhook.callback_url,
        webhook_app_id=parsed.webhook.app_id,
        otp_code=parsed.otp_code,
        local_paths=dict(parsed.local_paths),
    )


def create_synology_client(
    config: SynologyConfig,
) -> AbstractAsyncContextManager[SynologyClient]:
    return create_client(
        base_url=config.url,
        username=config.username,
        password=config.password,
        otp_code=config.otp_code,
    )
