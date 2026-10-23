from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any
from unittest import TestCase

from wcpan.drive.plugin import check_settings, get_settings, render_settings


@asynccontextmanager
async def _factory(
    context: object,
    *,
    url: Annotated[str, "Base URL.\nWith scheme."],
    interval: Annotated[float, "Seconds."] = 60.0,
    watch: bool = True,
    otp: str | None = None,
    paths: Mapping[str, str] | None = None,
    names: list[str] | None = None,
) -> AsyncGenerator[None]:
    yield


async def _open(context: object, *, url: str, **settings: Any) -> None:
    pass


class TestGetSettings(TestCase):
    def test_reads_keyword_only_parameters_through_decorators(self):
        # when
        settings = get_settings(_factory)
        # then
        self.assertEqual(
            [_.name for _ in settings],
            ["url", "interval", "watch", "otp", "paths", "names"],
        )
        url, interval, watch = settings[:3]
        self.assertIs(url.type, str)
        self.assertEqual(url.help, "Base URL.\nWith scheme.")
        self.assertTrue(url.required)
        self.assertIs(interval.type, float)
        self.assertEqual(interval.default, 60.0)
        self.assertFalse(interval.required)
        self.assertEqual(watch.help, "")


class TestCheckSettings(TestCase):
    def test_accepts_and_converts(self):
        # when
        rv = check_settings(
            _factory,
            {"url": "u", "interval": 5, "otp": None, "paths": {"/a": "/b"}},
        )
        # then
        self.assertEqual(
            rv, {"url": "u", "interval": 5.0, "otp": None, "paths": {"/a": "/b"}}
        )
        self.assertIsInstance(rv["interval"], float)

    def test_rejects_unknown_settings(self):
        # when / then
        with self.assertRaisesRegex(ValueError, "unknown settings: extra, webhook"):
            check_settings(_factory, {"url": "u", "webhook": {}, "extra": 1})

    def test_keeps_unknown_settings_for_open_factories(self):
        # when / then
        self.assertEqual(
            check_settings(_open, {"url": "u", "x": 1}), {"url": "u", "x": 1}
        )

    def test_rejects_missing_settings(self):
        # when / then
        with self.assertRaisesRegex(ValueError, "missing settings: url"):
            check_settings(_factory, {})

    def test_rejects_wrong_types(self):
        # given
        for settings, pattern in (
            ({"url": 1}, "url must be str"),
            ({"url": None}, "url must be str"),
            ({"interval": True}, "interval must be float"),
            ({"watch": "yes"}, "watch must be bool"),
            ({"otp": 1}, "otp must be str"),
            ({"paths": {"/a": 1}}, r"paths\./a must be str"),
            ({"paths": ["/a"]}, "paths must be"),
            ({"names": ["a", 2]}, r"names\[1\] must be str"),
        ):
            # when / then
            with (
                self.subTest(settings=settings),
                self.assertRaisesRegex(ValueError, pattern),
            ):
                check_settings(_factory, {"url": "u", **settings})


class TestRenderSettings(TestCase):
    def test_required_are_empty_and_optional_are_commented(self):
        # when
        lines = render_settings(get_settings(_factory), indent=2)
        # then
        self.assertEqual(
            lines,
            [
                "  # Base URL.",
                "  # With scheme.",
                "  url:  # required",
                "  # Seconds.",
                "  # interval: 60.0",
                "  # watch: true",
                "  # otp: null",
                "  # paths: null",
                "  # names: null",
            ],
        )
