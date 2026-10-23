"""The generic server must not depend on a backend or on client packages."""

import ast
from pathlib import Path
from unittest import TestCase


_SERVER_SRC = Path(__file__).parents[1] / "src/wcpan/drive/server"
_FORBIDDEN = ("wcpan.synology", "wcpan.drive.server.synology", "wcpan.drive.client")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


class TestGenericServerImports(TestCase):
    def test_no_backend_or_client_imports(self) -> None:
        modules = sorted(_SERVER_SRC.rglob("*.py"))
        self.assertTrue(modules)
        for path in modules:
            for name in _imports(path):
                with self.subTest(module=str(path.relative_to(_SERVER_SRC))):
                    self.assertFalse(
                        any(name == f or name.startswith(f + ".") for f in _FORBIDDEN),
                        f"imports {name}",
                    )
