"""Synology backend settings and the API client built from them."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from wcpan.synology.lib import create_client
from wcpan.synology.types import SynologyClient


@dataclass(frozen=True, kw_only=True)
class SynologyConfig:
    url: str
    username: str
    password: str
    public_url: str
    webhook_app_id: str
    otp_code: str | None = None
    # Parent of the spool directory for resumable uploads; None for the system's.
    upload_tmp_dir: str | None = None


def create_synology_client(
    config: SynologyConfig,
) -> AbstractAsyncContextManager[SynologyClient]:
    return create_client(
        base_url=config.url,
        username=config.username,
        password=config.password,
        otp_code=config.otp_code,
    )
