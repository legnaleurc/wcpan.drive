from importlib.metadata import version

from ._lib import get_image_info as get_image_info
from ._lib import get_media_info as get_media_info
from ._lib import get_mime_type as get_mime_type
from ._lib import get_video_info as get_video_info


__version__ = version("wcpan-drive-client-media")
__all__ = (
    "get_image_info",
    "get_media_info",
    "get_mime_type",
    "get_video_info",
)
