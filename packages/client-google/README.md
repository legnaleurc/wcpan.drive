# wcpan.drive.client.google

`Backend` extension for wcpan.drive which provides Google Drive support.

Please use `wcpan.drive.client.google.create_backend` to create the backend.

## Requirement

Need a `client_secret.json` file which can be downloaded from
Google Developer Console.

## Example

```python
from functools import partial

from wcpan.drive.client.api import create_drive
from wcpan.drive.client.google import create_backend


async def main():
    # Your API credential.
    client_secret = "/path/to/client_secret.json"
    # Stores access token and refresh token.
    oauth_token = "/path/to/oauth_token.json"

    create_google_backend = partial(
        create_backend,
        client_secret=client_secret,
        oauth_token=oauth_token,
    )

    async with create_drive(
        backend=create_google_backend,
        cache=...,
    ) as drive:
        ...
```
