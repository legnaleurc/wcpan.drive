"""Translation of wcpan-synology errors for node operations."""

from unittest import TestCase

from wcpan.drive.server.backend.synology._errors import operation_error
from wcpan.drive.server.exceptions import (
    OperationConflictError,
    OperationInvalidError,
    OperationNotFoundError,
    OperationNotSupportedError,
    OperationStorageFullError,
    OperationUnavailableError,
)
from wcpan.synology.exceptions import (
    SynologyApiError,
    SynologyAuthenticationError,
    SynologyBusyError,
    SynologyConflictError,
    SynologyDirectoryNotFoundError,
    SynologyInvalidNameError,
    SynologyLockedError,
    SynologyNotFoundError,
    SynologyPermissionError,
    SynologyProtocolError,
    SynologyQuotaExceededError,
    SynologyStorageFullError,
)


class TestOperationError(TestCase):
    def assert_maps(self, error: SynologyApiError, expected: type[Exception]) -> None:
        translated = operation_error(error)
        self.assertIs(type(translated), expected)
        self.assertEqual(str(translated), str(error))

    def test_conflict(self) -> None:
        self.assert_maps(SynologyConflictError("taken"), OperationConflictError)

    def test_permission_is_not_supported(self) -> None:
        self.assert_maps(SynologyPermissionError("denied"), OperationNotSupportedError)

    def test_invalid_name(self) -> None:
        self.assert_maps(SynologyInvalidNameError("bad"), OperationInvalidError)

    def test_missing_node_is_not_found(self) -> None:
        self.assert_maps(SynologyNotFoundError("gone"), OperationNotFoundError)

    def test_missing_folder_is_not_found(self) -> None:
        self.assert_maps(SynologyDirectoryNotFoundError("gone"), OperationNotFoundError)

    def test_full_storage(self) -> None:
        self.assert_maps(SynologyStorageFullError("full"), OperationStorageFullError)

    def test_exceeded_quota_is_full_storage(self) -> None:
        self.assert_maps(SynologyQuotaExceededError("quota"), OperationStorageFullError)

    def test_transient_and_unknown_failures_are_unavailable(self) -> None:
        for error in (
            SynologyBusyError("busy"),
            SynologyLockedError("locked"),
            SynologyAuthenticationError("session"),
            SynologyProtocolError("garbled"),
            SynologyApiError("unknown", error_code=9999),
        ):
            with self.subTest(error=type(error).__name__):
                self.assert_maps(error, OperationUnavailableError)
