from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from wcpan.drive.client.media import get_media_info
from wcpan.drive.client.types import MediaInfo


_LIB = "wcpan.drive.client.media._lib"
_PATH = Path("/tmp/file")
_IMAGE = MediaInfo.image(width=1, height=2)
_VIDEO = MediaInfo.video(width=3, height=4, ms_duration=5)


class GetMediaInfoTestCase(TestCase):
    def _get(self, mime_type: str) -> MediaInfo | None:
        with (
            patch(f"{_LIB}.get_mime_type", return_value=mime_type),
            patch(f"{_LIB}.get_image_info", return_value=_IMAGE),
            patch(f"{_LIB}.get_video_info", return_value=_VIDEO),
        ):
            return get_media_info(_PATH)

    def testImage(self) -> None:
        self.assertIs(self._get("image/png"), _IMAGE)

    def testVideo(self) -> None:
        self.assertIs(self._get("video/mp4"), _VIDEO)

    def testOther(self) -> None:
        self.assertIsNone(self._get("text/plain"))

    def testUnknown(self) -> None:
        self.assertIsNone(self._get(""))
