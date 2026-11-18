"""Translate Drive API errors into the server's common errors.

The categories come from ``wcpan-synology``'s Files error model. A category
names what failed, not whether a retry helps, so the choice of common error is
made here. Callers handle conflicts first, since resolving the existing node
needs their context, and network errors themselves, since those map to each
caller's own transient error.
"""

from wcpan.drive.server.exceptions import (
    OperationConflictError,
    OperationInvalidError,
    OperationNotFoundError,
    OperationNotSupportedError,
    OperationStorageFullError,
    OperationUnavailableError,
    UploadNameTooLongError,
    UploadParentGoneError,
    UploadPermanentError,
    UploadTransientError,
)
from wcpan.synology.exceptions import (
    SynologyApiError,
    SynologyBusyError,
    SynologyConflictError,
    SynologyInvalidNameError,
    SynologyInvalidOperationError,
    SynologyNameTooLongError,
    SynologyNotFoundError,
    SynologyPathTooLongError,
    SynologyPermissionError,
    SynologyStorageFullError,
    SynologyUnsupportedError,
)


def operation_error(e: SynologyApiError) -> Exception:
    """The ``NodeOperations`` error for a failed create, rename, move, or delete."""
    if isinstance(e, SynologyConflictError):
        return OperationConflictError(str(e))
    if isinstance(e, (SynologyPermissionError, SynologyUnsupportedError)):
        return OperationNotSupportedError(str(e))
    if isinstance(
        e,
        (
            SynologyInvalidNameError,
            SynologyNameTooLongError,
            SynologyPathTooLongError,
            SynologyInvalidOperationError,
        ),
    ):
        return OperationInvalidError(str(e))
    if isinstance(e, SynologyNotFoundError):
        return OperationNotFoundError(str(e))
    if isinstance(e, SynologyStorageFullError):
        return OperationStorageFullError(str(e))
    # Busy, locked, authentication and protocol failures, and unknown codes.
    return OperationUnavailableError(str(e))


def upload_error(e: SynologyApiError, name: str) -> Exception:
    """The ``Uploader`` error for a failed conflict lookup or upload of *name*."""
    if isinstance(e, SynologyNameTooLongError):
        return UploadNameTooLongError(name)
    if isinstance(e, SynologyNotFoundError):
        return UploadParentGoneError(f"The folder for {name!r} no longer exists")
    if isinstance(e, SynologyBusyError):
        return UploadTransientError(str(e))
    if isinstance(e, SynologyPermissionError):
        return OperationNotSupportedError(str(e))
    if isinstance(e, SynologyInvalidNameError):
        return OperationInvalidError(str(e))
    # Full storage, too large, too long a path, locks, and unknown codes.
    return UploadPermanentError(str(e))
