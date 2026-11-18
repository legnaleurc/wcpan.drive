"""Start a real server process for tests of how it ends."""

import asyncio
import sys
from pathlib import Path

from wcpan.drive.server._config import CONFIG_VERSION


_PACKAGE = Path(__file__).parent.parent.parent
_START = "from wcpan.drive.server._main import main; main()"
_FACTORY = "tests.integration._failing_backend.create_backend"


async def start_server(
    directory: str, *, phase: str, capture_output: bool = True
) -> asyncio.subprocess.Process:
    """A server in its own session, with ``_failing_backend`` failing at *phase*.

    Leftover processes keep captured output open, so a test that checks for them
    turns *capture_output* off.
    """
    output = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    config = Path(directory) / "server.yaml"
    config.write_text(
        f"version: {CONFIG_VERSION}\n"
        "server:\n"
        f"  database_url: {directory}/mirror.sqlite\n"
        "  host: 127.0.0.1\n"
        "  port: 0\n"
        "backends:\n"
        "  failing:\n"
        f"    type: {_FACTORY}\n"
        f"    phase: {phase}\n"
        "mounts:\n"
        "  docs:\n"
        "    backend: failing\n"
        "    source: docs\n"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _START,
        "--config",
        str(config),
        "serve",
        cwd=_PACKAGE,
        stdout=output,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
