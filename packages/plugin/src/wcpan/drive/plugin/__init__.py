from importlib.metadata import version

from ._discovery import factory_path as factory_path
from ._discovery import find_plugin as find_plugin
from ._discovery import list_plugins as list_plugins
from ._render import render_settings as render_settings
from ._settings import Setting as Setting
from ._settings import check_settings as check_settings
from ._settings import get_settings as get_settings


__version__ = version("wcpan-drive-plugin")
__all__ = (
    "Setting",
    "check_settings",
    "factory_path",
    "find_plugin",
    "get_settings",
    "list_plugins",
    "render_settings",
)
