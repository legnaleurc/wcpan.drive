from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import yaml
from decoy import Decoy, matchers
from wcpan.drive.client.api import create_multi_drive
from wcpan.drive.client.config import create_drive_from_config
from wcpan.drive.client.types import Drive, SourceConfig


_CFG = "wcpan.drive.client.config._cfg"


def _source(name: str = "google") -> dict[str, Any]:
    return {
        "name": name,
        "backend": [
            {"name": "pkg.backend.create_backend", "kwargs": {"token": "t"}},
            {"name": "pkg.crypt.create_middleware"},
        ],
        "cache": f"/cache/{name}.sqlite",
    }


def _create_backend(*, token: str) -> Any:
    raise AssertionError("not called: create_multi_drive is mocked")


def _create_middleware(service: Any) -> Any:
    raise AssertionError("not called: create_multi_drive is mocked")


class _ConfigTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._path = Path(tmp.name) / "config.yaml"

        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)
        self._import_module = self.decoy.mock(func=import_module)
        self._create_multi_drive = self.decoy.mock(func=create_multi_drive)
        for target, mock in (
            (f"{_CFG}.import_module", self._import_module),
            (f"{_CFG}.create_multi_drive", self._create_multi_drive),
        ):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, data: object) -> None:
        self._path.write_text(yaml.safe_dump(data))


class CreateDriveFromConfigTestCase(_ConfigTestCase):
    async def _assert_invalid(self, data: object) -> None:
        self._write(data)
        with self.assertRaises(ValueError):
            async with create_drive_from_config(self._path):
                pass
        self.decoy.verify(self._import_module(matchers.Anything()), times=0)
        self.decoy.verify(
            self._create_multi_drive(sources=matchers.Anything()), times=0
        )

    async def testValid(self) -> None:
        # given
        # Plain functions, not spies: settings are read from their annotations.
        for name, fn, factory in (
            ("pkg.backend", "create_backend", _create_backend),
            ("pkg.crypt", "create_middleware", _create_middleware),
        ):
            module = ModuleType(name)
            setattr(module, fn, factory)
            self.decoy.when(self._import_module(name)).then_return(module)
        drive = self.decoy.mock(cls=Drive)
        sources = matchers.Captor()
        self.decoy.when(self._create_multi_drive(sources=sources)).then_enter_with(
            drive
        )
        self._write({"version": 1, "sources": [_source("google"), _source("local")]})

        # when
        async with create_drive_from_config(self._path) as rv:
            # then
            self.assertIs(rv, drive)

        captured: list[SourceConfig] = sources.value
        self.assertEqual([_.name for _ in captured], ["google", "local"])
        self.assertEqual(
            [_.cache_path for _ in captured],
            ["/cache/google.sqlite", "/cache/local.sqlite"],
        )

    async def testWrongVersion(self) -> None:
        # when / then
        await self._assert_invalid({"version": 2, "sources": [_source()]})

    async def testNotMapping(self) -> None:
        # when / then
        await self._assert_invalid([])

    async def testEmptySources(self) -> None:
        # when / then
        await self._assert_invalid({"version": 1, "sources": []})

    async def testDuplicateNames(self) -> None:
        # when / then
        await self._assert_invalid({"version": 1, "sources": [_source(), _source()]})

    async def testMissingName(self) -> None:
        # given
        source = _source()
        del source["name"]
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testNameWithColon(self) -> None:
        # given
        source = _source()
        source["name"] = "a:b"
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testEmptyChain(self) -> None:
        # given
        source = _source()
        source["backend"] = []
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testMissingChain(self) -> None:
        # given
        source = _source()
        del source["backend"]
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testUndottedFunctionName(self) -> None:
        # given
        source = _source()
        source["backend"][0]["name"] = "create_backend"
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testInvalidArgs(self) -> None:
        # given
        source = _source()
        source["backend"][0]["args"] = "token"
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})

    async def testInvalidCachePath(self) -> None:
        for cache in (None, "", [{"name": "pkg.cache.create_cache"}]):
            with self.subTest(cache=cache):
                # given
                source = _source()
                source["cache"] = cache
                # when / then
                await self._assert_invalid({"version": 1, "sources": [source]})

    async def testInvalidKwargs(self) -> None:
        # given
        source = _source()
        source["backend"][0]["kwargs"] = ["token"]
        # when / then
        await self._assert_invalid({"version": 1, "sources": [source]})


def _backend_factory(*, token: str, timeout: float = 1.0) -> None:
    raise AssertionError("factories are not called while loading")


def _middleware_factory(backend: object) -> None:
    raise AssertionError("factories are not called while loading")


class FactorySettingsTestCase(_ConfigTestCase):
    """Factory kwargs are checked against the factory's keyword-only settings."""

    def _modules(self) -> None:
        backend = ModuleType("pkg.backend")
        setattr(backend, "create_backend", _backend_factory)
        crypt = ModuleType("pkg.crypt")
        setattr(crypt, "create_middleware", _middleware_factory)
        self.decoy.when(self._import_module("pkg.backend")).then_return(backend)
        self.decoy.when(self._import_module("pkg.crypt")).then_return(crypt)

    async def _assert_rejected(self, kwargs: dict[str, Any], message: str) -> None:
        self._modules()
        source = _source()
        source["backend"][0]["kwargs"] = kwargs
        self._write({"version": 1, "sources": [source]})
        with self.assertRaisesRegex(ValueError, message):
            async with create_drive_from_config(self._path):
                pass
        self.decoy.verify(
            self._create_multi_drive(sources=matchers.Anything()), times=0
        )

    async def testUnknownSetting(self) -> None:
        await self._assert_rejected(
            {"token": "t", "tokne": "x"}, r"backend\[0\].*unknown settings: tokne"
        )

    async def testMissingSetting(self) -> None:
        await self._assert_rejected({}, r"missing settings: token")

    async def testMistypedSetting(self) -> None:
        await self._assert_rejected({"token": 1}, r"token must be str")

    async def testCheckedSettingsArePassed(self) -> None:
        # given
        self._modules()
        source = _source()
        source["backend"][0]["kwargs"] = {"token": "t", "timeout": 3}
        self._write({"version": 1, "sources": [source]})
        self.decoy.when(
            self._create_multi_drive(sources=matchers.Anything())
        ).then_enter_with(self.decoy.mock(cls=Drive))

        # when
        with patch(f"{_CFG}.compose_service") as compose_service:
            async with create_drive_from_config(self._path):
                pass

        # then
        backend, _middleware = compose_service.call_args.args
        self.assertIs(backend.func, _backend_factory)
        self.assertEqual(backend.keywords, {"token": "t", "timeout": 3.0})
        self.assertIsInstance(backend.keywords["timeout"], float)
