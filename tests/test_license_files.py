"""Every workspace member ships the same license text as the repository root."""

from pathlib import Path
from unittest import TestCase


_ROOT = Path(__file__).resolve().parent.parent


class TestLicenseFiles(TestCase):
    def test_every_package_ships_the_root_license(self) -> None:
        # given
        expected = (_ROOT / "LICENSE.txt").read_bytes()
        packages = sorted(
            p.parent for p in (_ROOT / "packages").glob("*/pyproject.toml")
        )
        self.assertTrue(packages)

        # then
        for package in packages:
            with self.subTest(package=package.name):
                self.assertEqual((package / "LICENSE.txt").read_bytes(), expected)
