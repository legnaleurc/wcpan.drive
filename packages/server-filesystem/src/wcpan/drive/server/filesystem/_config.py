"""Filesystem backend settings.

Mount sources are absolute host directories. Local media probing reads the same
paths unless ``local_paths`` maps them elsewhere.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class FilesystemConfig:
    # Seconds between full rescans after the startup scan.
    rescan_interval: float = 3600.0
    # Re-read changed directories as inotify reports them.
    watch: bool = True
    # Reject every change to the mounted directories.
    read_only: bool = False
    # Source path prefix to host path for media probing; ``None`` probes the
    # source paths themselves.
    local_paths: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.rescan_interval <= 0:
            raise ValueError("rescan_interval must be positive")


def check_mounts(mounts: Mapping[str, str]) -> dict[str, str]:
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
