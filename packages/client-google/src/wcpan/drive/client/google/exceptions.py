from wcpan.drive.client.exceptions import DriveError


__all__ = (
    "AuthenticationError",
    "CredentialFileError",
    "TokenFileError",
    "DownloadAbusiveFileError",
    "InvalidAbuseFlagError",
    "UploadError",
)


class AuthenticationError(DriveError):
    pass


class CredentialFileError(DriveError):
    pass


class TokenFileError(DriveError):
    pass


class DownloadAbusiveFileError(DriveError):
    pass


class InvalidAbuseFlagError(DriveError):
    pass


class UploadError(DriveError):
    pass
