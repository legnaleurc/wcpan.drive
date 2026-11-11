"""Filesystem backend settings.

Mount sources are absolute host directories. Local media probing reads the same
paths unless ``local_paths`` maps them elsewhere.
"""

import os
from dataclasses import dataclass, replace

from wcpan.drive.server.backend import Mounts


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


def check_mounts(mounts: Mounts) -> Mounts:
    """*mounts* with normalized sources; rejects relative and nested sources."""
    for name, mount in mounts.items():
        if not os.path.isabs(mount.source):
            raise ValueError(f"Mount {name!r} source {mount.source!r} is not absolute")
    normalized = Mounts(
        replace(m, source=os.path.normpath(m.source)) for m in mounts.values()
    )
    sources = sorted(m.source for m in normalized.values())
    for i, a in enumerate(sources):
        for b in sources[i + 1 :]:
            if b == a or b.startswith(a.rstrip("/") + "/"):
                raise ValueError(
                    f"Mount source {b!r} is inside {a!r}; nested mounts are not supported"
                )
    return normalized
