import inspect
from collections.abc import Callable, Sequence
from importlib.metadata import EntryPoint

from wcpan.drive.plugin import factory_path, find_plugin, get_settings, render_settings

from ._cfg import VERSION


BACKEND_GROUP = "wcpan.drive.client.backend"
PLUGIN_GROUPS = (BACKEND_GROUP,)


type _Plugin = tuple[EntryPoint, Callable[..., object]]


def create_config_scaffold(plugins: Sequence[str]) -> str:
    """A commented config with one source built from installed plugins.

    Each of *plugins* names an installed plugin by entry point name, module, or
    factory path. A factory that takes a positional argument is middleware; it
    follows the one backend it wraps, in the given order. Required settings
    and the cache path are left empty to fill in.

    Raises:
        ValueError: If a plugin is not installed or ambiguous, or the backend is
            not given exactly once.
    """
    chains: dict[str, list[_Plugin]] = {_: [] for _ in PLUGIN_GROUPS}
    for selector in plugins:
        ep = find_plugin(PLUGIN_GROUPS, selector)
        chains[ep.group].append((ep, ep.load()))
    backend = _chain(chains[BACKEND_GROUP], "backend")

    lines = [
        f"version: {VERSION}",
        "sources:",
        f"  - name: {backend[0][0].name}",
        "    backend:",
        *_render_chain(backend),
        "    # Path to the SQLite metadata cache file.",
        "    cache:  # required",
    ]
    return "\n".join(lines) + "\n"


def _chain(plugins: list[_Plugin], role: str) -> list[_Plugin]:
    bases = [_ for _ in plugins if not _is_middleware(_[1])]
    if len(bases) != 1:
        found = ", ".join(_[0].name for _ in bases) or "none"
        raise ValueError(f"give exactly one {role} plugin, got: {found}")
    return bases + [_ for _ in plugins if _is_middleware(_[1])]


def _is_middleware(factory: Callable[..., object]) -> bool:
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    parameters = inspect.signature(factory).parameters.values()
    return any(_.kind in positional for _ in parameters)


def _render_chain(plugins: list[_Plugin]) -> list[str]:
    lines: list[str] = []
    for ep, factory in plugins:
        lines.append(f"      - name: {factory_path(ep)}")
        settings = get_settings(factory)
        if not settings:
            continue
        # A kwargs mapping with every setting commented out would be null.
        header = "kwargs:" if any(_.required for _ in settings) else "# kwargs:"
        lines.append(f"        {header}")
        lines.extend(render_settings(settings, indent=10))
    return lines
