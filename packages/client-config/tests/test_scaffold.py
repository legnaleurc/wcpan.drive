from importlib.metadata import EntryPoint
from typing import Annotated
from unittest import TestCase
from unittest.mock import patch

import yaml
from wcpan.drive.client.config import create_config_scaffold
from wcpan.drive.client.config._cfg import (
    _parse_main,  # pyright: ignore[reportPrivateUsage]
)


async def create_backend(*, token: Annotated[str, "Token file."]) -> None:
    pass


async def create_middleware(backend: object, *, level: int = 1) -> None:
    pass


async def create_other_middleware(backend: object) -> None:
    pass


async def create_cache(*, dsn: str) -> None:
    pass


_PLUGINS = {
    "wcpan.drive.client.backend": [
        EntryPoint("drive", "tests.test_scaffold:create_backend", "x"),
        EntryPoint("other", "tests.test_scaffold:create_backend", "x"),
        EntryPoint("mid", "tests.test_scaffold:create_middleware", "x"),
        EntryPoint("mid2", "tests.test_scaffold:create_other_middleware", "x"),
    ],
    "wcpan.drive.client.cache": [
        EntryPoint("cache", "tests.test_scaffold:create_cache", "x"),
    ],
}


def _entry_points(*, group: str) -> list[EntryPoint]:
    return [EntryPoint(_.name, _.value, group) for _ in _PLUGINS.get(group, [])]


class CreateConfigScaffoldTestCase(TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch("wcpan.drive.plugin._discovery.entry_points", _entry_points)
        )

    def test_backend_comes_first_and_middleware_keeps_order(self) -> None:
        # when
        text = create_config_scaffold(["mid2", "cache", "mid", "drive"])
        # then
        self.assertEqual(
            text,
            """\
version: 4
sources:
  - name: drive
    backend:
      - name: tests.test_scaffold.create_backend
        kwargs:
          # Token file.
          token:  # required
      - name: tests.test_scaffold.create_other_middleware
      - name: tests.test_scaffold.create_middleware
        # kwargs:
          # level: 1
    cache:
      - name: tests.test_scaffold.create_cache
        kwargs:
          dsn:  # required
""",
        )

    def test_scaffold_parses_once_filled(self) -> None:
        # when
        raw = yaml.safe_load(create_config_scaffold(["drive", "mid", "cache"]))
        # then
        self.assertEqual(raw["sources"][0]["backend"][0]["kwargs"], {"token": None})
        self.assertNotIn("kwargs", raw["sources"][0]["backend"][1])
        # when
        (source,) = _parse_main(raw)
        # then
        self.assertEqual(
            [_.name for _ in source.backend],
            [
                "tests.test_scaffold.create_backend",
                "tests.test_scaffold.create_middleware",
            ],
        )

    def test_backend_and_cache_are_required_once(self) -> None:
        # given
        for plugins, pattern in (
            (["cache"], "one backend plugin, got: none"),
            (["mid", "cache"], "one backend plugin, got: none"),
            (["drive", "other", "cache"], "one backend plugin, got: drive, other"),
            (["drive"], "one cache plugin, got: none"),
            (["drive", "cache", "missing"], "is its package installed"),
        ):
            with self.subTest(plugins=plugins):
                # when / then
                with self.assertRaisesRegex(ValueError, pattern):
                    create_config_scaffold(plugins)
