"""Errors a backend raises or catches.

Backends import the server only through ``types``, ``exceptions``, and
``lib``; the rest of the package is private.
"""

from ._services.ids import UnknownNodeError as UnknownNodeError
from ._services.media.enricher import MediaEnrichmentError as MediaEnrichmentError
from ._services.media.probe.source import MediaProbeError as MediaProbeError
from ._services.media.probe.source import MediaSourceError as MediaSourceError
from ._services.media.probe.source import ProbeUnavailableError as ProbeUnavailableError
from ._services.media.probing import ProbesNotDeclaredError as ProbesNotDeclaredError
from ._services.operations import LocateUndecidedError as LocateUndecidedError
from ._services.operations import OperationConflictError as OperationConflictError
from ._services.operations import OperationInvalidError as OperationInvalidError
from ._services.operations import OperationNotFoundError as OperationNotFoundError
from ._services.operations import (
    OperationNotSupportedError as OperationNotSupportedError,
)
from ._services.operations import (
    OperationStorageFullError as OperationStorageFullError,
)
from ._services.operations import OperationUnavailableError as OperationUnavailableError
from ._services.sync.service import WriterStoppedError as WriterStoppedError
from ._services.upload.service import UploadConflictError as UploadConflictError
from ._services.upload.service import UploadInvalidMediaError as UploadInvalidMediaError
from ._services.upload.service import UploadNameTooLongError as UploadNameTooLongError
from ._services.upload.service import UploadParentGoneError as UploadParentGoneError
from ._services.upload.service import UploadPermanentError as UploadPermanentError
from ._services.upload.service import UploadTransientError as UploadTransientError


__all__ = (
    "MediaProbeError",
    "ProbeUnavailableError",
    "ProbesNotDeclaredError",
    "WriterStoppedError",
    "LocateUndecidedError",
    "MediaEnrichmentError",
    "MediaSourceError",
    "OperationConflictError",
    "OperationInvalidError",
    "OperationNotFoundError",
    "OperationNotSupportedError",
    "OperationStorageFullError",
    "OperationUnavailableError",
    "UnknownNodeError",
    "UploadConflictError",
    "UploadInvalidMediaError",
    "UploadNameTooLongError",
    "UploadParentGoneError",
    "UploadPermanentError",
    "UploadTransientError",
)
