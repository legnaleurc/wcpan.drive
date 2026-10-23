from wcpan.drive.client.exceptions import DriveError


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
