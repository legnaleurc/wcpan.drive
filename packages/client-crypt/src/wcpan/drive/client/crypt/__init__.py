from importlib.metadata import version

from ._service import create_middleware as create_middleware


__version__ = version("wcpan-drive-client-crypt")
__all__ = ("create_middleware",)
