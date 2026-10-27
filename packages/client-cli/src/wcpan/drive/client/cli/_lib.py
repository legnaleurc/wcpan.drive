import sys
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from pymediainfo import MediaInfo as MediaInfo_
from wcpan.drive.client.types import MediaInfo


def cout(*values: object) -> None:
    print(*values, file=sys.stdout, flush=True)


def cerr(*values: object) -> None:
    print(*values, file=sys.stderr, flush=True)


def print_as_yaml(data: Any) -> None:
    yaml.safe_dump(
        data,
        stream=sys.stdout,
        allow_unicode=True,
        encoding=sys.stdout.encoding,
        default_flow_style=False,
    )


def get_image_info(local_path: Path) -> MediaInfo:
    media_info = MediaInfo_.parse(
        local_path, mediainfo_options={"File_TestContinuousFileNames": "0"}
    )
    try:
        track = media_info.image_tracks[0]
    except IndexError as e:
        raise RuntimeError("not an image") from e

    width = track.width
    height = track.height
    if not isinstance(width, int):
        raise RuntimeError(f"invalid width: {width}")
    if not isinstance(height, int):
        raise RuntimeError(f"invalid height: {height}")

    return MediaInfo.image(width=track.width, height=track.height)


def get_video_info(local_path: Path) -> MediaInfo:
    media_info = MediaInfo_.parse(local_path)
    try:
        container = media_info.general_tracks[0]
    except IndexError as e:
        raise RuntimeError("not a media") from e
    try:
        video = media_info.video_tracks[0]
    except IndexError as e:
        raise RuntimeError("not a video") from e

    width = video.width
    height = video.height
    ms_duration = container.duration

    if isinstance(ms_duration, str):
        ms_duration = int(float(ms_duration))

    if not isinstance(width, int):
        raise RuntimeError(f"invalid width: {width}")
    if not isinstance(height, int):
        raise RuntimeError(f"invalid height: {height}")
    if not isinstance(ms_duration, int):
        raise RuntimeError(f"invalid duration: {ms_duration}")

    return MediaInfo.video(width=width, height=height, ms_duration=ms_duration)


def get_mime_type(local_path: Path) -> str:
    import magic

    return magic.from_file(local_path, mime=True)  # type: ignore


def get_media_info(local_path: Path) -> MediaInfo | None:
    mime_type = get_mime_type(local_path)
    if not mime_type:
        return None

    if mime_type.startswith("image/"):
        return get_image_info(local_path)

    if mime_type.startswith("video/"):
        return get_video_info(local_path)

    return None


def create_executor() -> Executor:
    from multiprocessing import get_start_method

    if get_start_method() == "spawn":
        return ProcessPoolExecutor(initializer=_initialize_worker)
    else:
        return ProcessPoolExecutor()


def _initialize_worker() -> None:
    from signal import SIG_IGN, SIGINT, signal

    signal(SIGINT, SIG_IGN)
