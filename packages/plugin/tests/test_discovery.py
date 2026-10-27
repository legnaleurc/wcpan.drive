from importlib.metadata import EntryPoint
from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.plugin import factory_path, find_plugin, list_plugins


_PLUGINS = {
    "a.backend": [
        EntryPoint("google", "pkg.google:create_backend", "a.backend"),
        EntryPoint("crypt", "pkg.crypt:create_middleware", "a.backend"),
    ],
    "a.cache": [
        EntryPoint("sqlite", "pkg.sqlite:create_cache", "a.cache"),
        EntryPoint("google", "pkg.google_cache:create_cache", "a.cache"),
    ],
}


def _entry_points(*, group: str) -> list[EntryPoint]:
    return _PLUGINS.get(group, [])


class TestDiscovery(TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch("wcpan.drive.plugin._discovery.entry_points", _entry_points)
        )

    def test_lists_sorted_plugins(self):
        plugins = list_plugins(["a.cache", "a.backend"])
        self.assertEqual(
            [(_.group, _.name) for _ in plugins],
            [
                ("a.backend", "crypt"),
                ("a.backend", "google"),
                ("a.cache", "google"),
                ("a.cache", "sqlite"),
            ],
        )

    def test_selects_by_name_module_or_path(self):
        for selector in ("crypt", "pkg.crypt", "pkg.crypt.create_middleware"):
            with self.subTest(selector=selector):
                ep = find_plugin(["a.backend", "a.cache"], selector)
                self.assertEqual(factory_path(ep), "pkg.crypt.create_middleware")

    def test_rejects_unknown_selector(self):
        with self.assertRaisesRegex(ValueError, "is its package installed"):
            find_plugin(["a.backend"], "pkg.nothing")

    def test_rejects_ambiguous_selector(self):
        with self.assertRaisesRegex(ValueError, "matches several plugins"):
            find_plugin(["a.backend", "a.cache"], "google")
        ep = find_plugin(["a.backend", "a.cache"], "pkg.google")
        self.assertEqual(ep.group, "a.backend")
