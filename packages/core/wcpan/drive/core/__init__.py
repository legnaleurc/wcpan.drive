from importlib.metadata import version

from ._drive import create_drive


__version__ = version(__package__ or __name__)
__all__ = ("create_drive",)
