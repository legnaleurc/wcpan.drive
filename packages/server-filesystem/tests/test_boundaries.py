"""The backend reaches mounts only through the backend contract."""

import ast
from pathlib import Path
from unittest import TestCase


_SRC = Path(__file__).parents[1] / "src"
# The core's private knowledge of the root; backends use `Mounts.root_id`.
_FORBIDDEN = ("wcpan.drive.server.lib.root",)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


class TestBackendImports(TestCase):
    def test_no_root_imports(self) -> None:
        # given
        modules = sorted(_SRC.rglob("*.py"))
        # then
        self.assertTrue(modules)
        for path in modules:
            for name in _imports(path):
                with self.subTest(module=str(path.relative_to(_SRC))):
                    self.assertFalse(
                        any(name == f or name.startswith(f + ".") for f in _FORBIDDEN),
                        f"imports {name}",
                    )
