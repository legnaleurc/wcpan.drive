from collections.abc import Sequence
from typing import Any

import yaml

from ._settings import Setting


def render_settings(settings: Sequence[Setting], *, indent: int) -> list[str]:
    """YAML lines for *settings*, each preceded by its help as comments.

    Required settings are left empty to fill in; optional ones are commented
    out with their defaults.
    """
    pad = " " * indent
    lines: list[str] = []
    for setting in settings:
        lines.extend(f"{pad}# {_}".rstrip() for _ in setting.help.splitlines())
        if setting.required:
            lines.append(f"{pad}{setting.name}:  # required")
        else:
            lines.append(f"{pad}# {setting.name}: {_inline(setting.default)}")
    return lines


def _inline(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=1 << 16)
    return text.removesuffix("\n").removesuffix("\n...")
