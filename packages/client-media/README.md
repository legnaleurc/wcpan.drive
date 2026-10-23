# wcpan.drive.client.media

Local file MIME type and media probing for wcpan.drive uploads.

Requires the native `libmagic` and `libmediainfo` libraries at runtime.

## Example Usage

```python
from pathlib import Path

from wcpan.drive.client.lib import upload_file_from_local
from wcpan.drive.client.media import get_media_info, get_mime_type


async def upload(drive, path: Path, parent):
    return await upload_file_from_local(
        drive,
        path,
        parent,
        mime_type=get_mime_type(path),
        media_info=get_media_info(path),
    )
```
