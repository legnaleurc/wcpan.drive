from collections.abc import Sequence
from importlib.metadata import EntryPoint, entry_points


def list_plugins(groups: Sequence[str]) -> list[EntryPoint]:
    """Installed plugins of *groups*, sorted by group and name."""
    rv = [ep for group in groups for ep in entry_points(group=group)]
    return sorted(rv, key=lambda _: (_.group, _.name))


def find_plugin(groups: Sequence[str], selector: str) -> EntryPoint:
    """The one installed plugin of *groups* that *selector* names.

    *selector* is an entry point name, the factory's module, or its dotted path.

    Raises:
        ValueError: If no plugin or more than one plugin matches.
    """
    matches = [
        ep
        for ep in list_plugins(groups)
        if selector in (ep.name, ep.module, factory_path(ep))
    ]
    if not matches:
        raise ValueError(f"no plugin matches {selector!r}; is its package installed?")
    if len(matches) > 1:
        found = ", ".join(f"{_.name} ({factory_path(_)})" for _ in matches)
        raise ValueError(f"{selector!r} matches several plugins: {found}")
    return matches[0]


def factory_path(ep: EntryPoint) -> str:
    """The dotted path configs use to name the factory of *ep*."""
    return f"{ep.module}.{ep.attr}"
