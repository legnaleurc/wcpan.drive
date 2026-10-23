from importlib.metadata import version

from ._cfg import create_drive_from_config as create_drive_from_config
from ._scaffold import PLUGIN_GROUPS as PLUGIN_GROUPS
from ._scaffold import create_config_scaffold as create_config_scaffold


__version__ = version("wcpan-drive-client-config")
__all__ = ("PLUGIN_GROUPS", "create_config_scaffold", "create_drive_from_config")
