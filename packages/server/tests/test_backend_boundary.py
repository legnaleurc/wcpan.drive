"""The core and the built-in backends meet only at the public backend API.

They share a distribution, so packaging does not keep them apart. Ruff's
banned-api rule misses relative imports, so this reads the imports directly.
"""

import ast
from collections.abc import Iterator
from pathlib import Path
from unittest import TestCase

import wcpan.drive.server.lib


_SERVER = "wcpan.drive.server"
_BACKENDS = f"{_SERVER}.backend"
_PUBLIC = tuple(f"{_SERVER}.{name}" for name in ("types", "lib", "exceptions"))
_PUBLIC_FOR_TESTS = (*_PUBLIC, f"{_SERVER}.testing")
_ROOT = Path(wcpan.drive.server.lib.__file__).parent
_CORE_INTERNALS = frozenset({"node_sync"})


def _imports(source: str, path: Path) -> Iterator[tuple[int, str]]:
    """Each module *source* imports, made absolute as if it were at *path*.

    ``from a import b`` yields ``a.b``, since ``b`` may be a module.
    """
    relative = path.relative_to(_ROOT).with_suffix("").parts
    package = [*_SERVER.split("."), *relative[:-1]]
    for node in ast.walk(ast.parse(source, str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = [node.module] if node.module else []
            if node.level:
                base = [*package[: len(package) - node.level + 1], *base]
            for alias in node.names:
                yield node.lineno, ".".join([*base, alias.name])


def _file_imports(path: Path) -> Iterator[tuple[int, str]]:
    return _imports(path.read_text(), path)


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _crosses_boundary(module: str, backend: str) -> bool:
    """Whether a module of *backend* may not import *module*."""
    if _within(module, backend) or not _within(module, _SERVER):
        return False
    return not any(_within(module, public) for public in _PUBLIC)


def _absolute_imports(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def _core_internals_used(path: Path) -> Iterator[tuple[int, str]]:
    """Uses of what ``CoreServices`` keeps for the core itself."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Attribute) and node.attr in _CORE_INTERNALS:
            yield node.lineno, node.attr


class TestBackendBoundary(TestCase):
    def test_backend_tests_use_only_the_public_core(self) -> None:
        # given
        tests = Path(__file__).parent / "backend"

        # when
        violations = [
            f"{path.relative_to(tests)}:{line}: {module}"
            for path in sorted(tests.rglob("*.py"))
            for line, module in _absolute_imports(path)
            if _within(module, _SERVER)
            and not _within(module, _BACKENDS)
            and not any(_within(module, public) for public in _PUBLIC_FOR_TESTS)
        ]

        # then
        self.assertEqual(violations, [])

    def test_backends_use_core_services_only_through_narrow_apis(self) -> None:
        # given
        backends = _ROOT / "backend"

        # when
        violations = [
            f"{path.relative_to(_ROOT)}:{line}: {name}"
            for path in sorted(backends.rglob("*.py"))
            for line, name in _core_internals_used(path)
        ]

        # then
        self.assertEqual(violations, [])

    def test_core_does_not_import_backends(self) -> None:
        # given
        backends = _ROOT / "backend"

        # when
        violations = [
            f"{path.relative_to(_ROOT)}:{line}: {module}"
            for path in sorted(_ROOT.rglob("*.py"))
            if not path.is_relative_to(backends)
            for line, module in _file_imports(path)
            if _within(module, _BACKENDS) or _within(module, "wcpan.synology")
        ]

        # then
        self.assertEqual(violations, [])

    def test_backends_import_only_the_public_core(self) -> None:
        # given
        names = sorted(
            p.name
            for p in (_ROOT / "backend").iterdir()
            if p.is_dir() and p.name != "__pycache__"
        )

        # when
        violations = [
            f"{path.relative_to(_ROOT)}:{line}: {module}"
            for name in names
            for path in sorted((_ROOT / "backend" / name).rglob("*.py"))
            for line, module in _file_imports(path)
            if _crosses_boundary(module, f"{_BACKENDS}.{name}")
        ]

        # then
        self.assertEqual(names, ["filesystem", "synology"])
        self.assertEqual(violations, [])

    def test_relative_imports_are_resolved(self) -> None:
        # given
        path = _ROOT / "backend" / "synology" / "_x.py"
        source = "from ..._services.ids import NodeIds\nfrom ._y import z\n"

        # when
        modules = [module for _, module in _imports(source, path)]

        # then
        self.assertEqual(
            modules,
            [f"{_SERVER}._services.ids.NodeIds", f"{_BACKENDS}.synology._y.z"],
        )
        self.assertTrue(_crosses_boundary(modules[0], f"{_BACKENDS}.synology"))
        self.assertFalse(_crosses_boundary(modules[1], f"{_BACKENDS}.synology"))
