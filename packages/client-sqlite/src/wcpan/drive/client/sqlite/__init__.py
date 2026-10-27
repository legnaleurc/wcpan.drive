from importlib.metadata import version

from ._service import create_cache as create_cache


__version__ = version("wcpan-drive-client-sqlite")
__all__ = ("create_cache",)
