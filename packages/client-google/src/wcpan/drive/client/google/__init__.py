from importlib.metadata import version

from ._service import create_backend as create_backend


__version__ = version("wcpan-drive-client-google")
__all__ = ("create_backend",)
