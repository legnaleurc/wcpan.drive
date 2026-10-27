from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

import yaml
from wcpan.drive.client.config import create_drive_from_config
from wcpan.drive.client.types import SourceConfig


_CFG = "wcpan.drive.client.config._cfg"


def _source(name: str = "google") -> dict[str, Any]:
    return {
        "name": name,
        "backend": [
            {"name": "pkg.backend.create_backend", "kwargs": {"token": "t"}},
            {"name": "pkg.crypt.create_middleware"},
        ],
        "cache": [{"name": "pkg.cache.create_cache", "args": ["dsn"]}],
    }


class CreateDriveFromConfigTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._path = Path(tmp.name) / "config.yaml"

        self._import_module = MagicMock()
        self._create_multi_drive = MagicMock()
        for target, mock in (
            (f"{_CFG}.import_module", self._import_module),
            (f"{_CFG}.create_multi_drive", self._create_multi_drive),
        ):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, data: object) -> None:
        self._path.write_text(yaml.safe_dump(data))

    async def _assert_invalid(self, data: object) -> None:
        self._write(data)
        with self.assertRaises(ValueError):
            async with create_drive_from_config(self._path):
                pass
        self._import_module.assert_not_called()
        self._create_multi_drive.assert_not_called()

    async def testValid(self) -> None:
        drive = object()
        self._create_multi_drive.return_value.__aenter__.return_value = drive
        self._write({"version": 4, "sources": [_source("google"), _source("local")]})

        async with create_drive_from_config(self._path) as rv:
            self.assertIs(rv, drive)

        self.assertEqual(
            [_.args[0] for _ in self._import_module.call_args_list],
            ["pkg.backend", "pkg.crypt", "pkg.cache"] * 2,
        )
        sources: list[SourceConfig] = self._create_multi_drive.call_args.kwargs[
            "sources"
        ]
        self.assertEqual([_.name for _ in sources], ["google", "local"])

    async def testWrongVersion(self) -> None:
        await self._assert_invalid({"version": 3, "sources": [_source()]})

    async def testNotMapping(self) -> None:
        await self._assert_invalid([])

    async def testEmptySources(self) -> None:
        await self._assert_invalid({"version": 4, "sources": []})

    async def testDuplicateNames(self) -> None:
        await self._assert_invalid({"version": 4, "sources": [_source(), _source()]})

    async def testMissingName(self) -> None:
        source = _source()
        del source["name"]
        await self._assert_invalid({"version": 4, "sources": [source]})

    async def testEmptyChain(self) -> None:
        source = _source()
        source["cache"] = []
        await self._assert_invalid({"version": 4, "sources": [source]})

    async def testMissingChain(self) -> None:
        source = _source()
        del source["backend"]
        await self._assert_invalid({"version": 4, "sources": [source]})

    async def testUndottedFunctionName(self) -> None:
        source = _source()
        source["backend"][0]["name"] = "create_backend"
        await self._assert_invalid({"version": 4, "sources": [source]})

    async def testInvalidArgs(self) -> None:
        source = _source()
        source["cache"][0]["args"] = "dsn"
        await self._assert_invalid({"version": 4, "sources": [source]})

    async def testInvalidKwargs(self) -> None:
        source = _source()
        source["backend"][0]["kwargs"] = ["token"]
        await self._assert_invalid({"version": 4, "sources": [source]})
