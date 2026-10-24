"""Filesystem section of the server config.

Mount sources are absolute host directories. Local media probing reads the same
paths, so ``local_paths`` defaults to mapping each source to itself.
"""

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dacite import Config, DaciteError, from_dict
from wcpan.drive.server.config import load_raw_config, parse_config
from wcpan.drive.server.types import ServerConfig


@dataclass(frozen=True, slots=True)
class RawFilesystemConfig:
    rescan_interval: float = 3600.0
    watch: bool = True
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class _RawFilesystemSection:
    filesystem: RawFilesystemConfig = field(default_factory=RawFilesystemConfig)


@dataclass(frozen=True, kw_only=True)
class FilesystemConfig:
    # Seconds between full rescans after the startup scan.
    rescan_interval: float
    # Re-read changed directories as inotify reports them.
    watch: bool = True
    # Reject every change to the mounted directories.
    read_only: bool = False


def parse_filesystem_config(raw: Any) -> FilesystemConfig:
    try:
        parsed = from_dict(
            data_class=_RawFilesystemSection,
            data=raw,
            config=Config(strict=False, cast=[float]),
        ).filesystem
    except DaciteError as e:
        raise ValueError(str(e)) from e
    if parsed.rescan_interval <= 0:
        raise ValueError("filesystem.rescan_interval must be positive")
    return FilesystemConfig(
        rescan_interval=parsed.rescan_interval,
        watch=parsed.watch,
        read_only=parsed.read_only,
    )


def check_mounts(mounts: dict[str, str]) -> dict[str, str]:
    """Normalized mount sources; rejects relative and nested sources."""
    normalized: dict[str, str] = {}
    for name, source in mounts.items():
        if not os.path.isabs(source):
            raise ValueError(f"Mount {name!r} source {source!r} is not absolute")
        normalized[name] = os.path.normpath(source)
    sources = sorted(normalized.values())
    for i, a in enumerate(sources):
        for b in sources[i + 1 :]:
            if b == a or b.startswith(a.rstrip("/") + "/"):
                raise ValueError(
                    f"Mount source {b!r} is inside {a!r}; nested mounts are not supported"
                )
    return normalized


def load_config(config_path: Path) -> tuple[ServerConfig, FilesystemConfig]:
    """Parse the generic settings first, so a version mismatch is reported first."""
    raw = load_raw_config(config_path)
    config = parse_config(raw)
    mounts = check_mounts(config.mounts)
    config = replace(
        config,
        mounts=mounts,
        local_paths=config.local_paths or {s: s for s in mounts.values()},
    )
    return config, parse_filesystem_config(raw)
