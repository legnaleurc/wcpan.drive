"""Small media files for probing tests."""

import struct
import zlib
from pathlib import Path


MEDIA = Path(__file__).parent / "media"
"""Fixture files; ``moov_at_end.mp4`` is a 32x24 video with its index last."""


def png(width: int, height: int) -> bytes:
    """A grayscale PNG of *width* by *height*."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\0" + b"\0" * width for _ in range(height)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


def _mediainfo_version() -> tuple[int, int] | None:
    import re
    import shutil
    import subprocess

    if shutil.which("mediainfo") is None:
        return None
    try:
        output = subprocess.run(
            ["mediainfo", "--Version"], capture_output=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(rb"MediaInfoLib - v(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else None


def requires_mediainfo[T](test: T) -> T:
    """Skip *test* unless a MediaInfo the runner accepts is installed."""
    from unittest import skipUnless

    from wcpan.drive.server._services.media.probe.runner import MIN_VERSION

    version = _mediainfo_version()
    return skipUnless(
        version is not None and version >= MIN_VERSION,
        f"needs mediainfo {MIN_VERSION[0]}.{MIN_VERSION[1]:02d} or later",
    )(test)
