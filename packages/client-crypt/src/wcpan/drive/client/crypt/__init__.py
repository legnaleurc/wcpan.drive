from importlib.metadata import version

from ._service import create_service as create_service


__version__ = version("wcpan-drive-client-crypt")
__all__ = ("create_service",)
