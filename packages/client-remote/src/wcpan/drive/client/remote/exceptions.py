from wcpan.drive.client.exceptions import DriveError


__all__ = (
    "SynologyAccessError",
    "SynologyNameTooLongError",
    "SynologyPermanentUploadError",
    "SynologyServerError",
    "SynologyUploadError",
)


class SynologyServerError(DriveError):
    """Raised when the wcpan.drive.client.remote server returns an error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SynologyAccessError(SynologyServerError):
    """Raised when the server refuses or throttles a request (401/403/429)."""


class SynologyUploadError(SynologyServerError):
    """Raised when an upload through the server fails."""

    def __init__(
        self,
        message: str,
        *,
        file_name: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message, status)
        self.file_name = file_name


class SynologyPermanentUploadError(SynologyUploadError):
    """An upload failure that cannot succeed by retrying unchanged."""


class SynologyNameTooLongError(SynologyPermanentUploadError):
    """The destination name was rejected as too long."""
