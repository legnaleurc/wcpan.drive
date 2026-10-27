from importlib.metadata import version

from ._cfg import create_drive_from_config as create_drive_from_config


__version__ = version("wcpan-drive-client-config")
__all__ = ("create_drive_from_config",)
