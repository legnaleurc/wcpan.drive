"""A killed server leaves no processes behind."""

import asyncio
import os
import signal
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from .._media import requires_mediainfo
from ._server_process import start_server


def _session_members(session: int) -> set[int]:
    """Running processes of *session*, not counting zombies."""
    members = set[int]()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # After the name: state, ppid, process group, session.
        fields = stat.rpartition(")")[2].split()
        if int(fields[3]) == session and fields[0] != "Z":
            members.add(int(entry.name))
    return members


@requires_mediainfo
class TestKilledServer(IsolatedAsyncioTestCase):
    async def test_worker_processes_exit_with_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # given: the server, its forkserver, tracker, and a pool worker
            process = await start_server(tmp, phase="none", capture_output=False)
            session = process.pid
            try:
                async with asyncio.timeout(30):
                    while len(_session_members(session)) < 4:
                        await asyncio.sleep(0.05)
                # when
                process.kill()
                await process.wait()
                # then
                async with asyncio.timeout(10):
                    while _session_members(session):
                        await asyncio.sleep(0.05)
            finally:
                try:
                    os.killpg(session, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
