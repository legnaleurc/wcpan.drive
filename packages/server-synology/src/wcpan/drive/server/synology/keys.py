from aiohttp.web import AppKey
from wcpan.synology import SynologyClient

from .mounts import MountRegistry
from .paths import SynologyPathService
from .webhook import WebhookQueue


SYNOLOGY_DRIVE_API_KEY = AppKey[SynologyClient]("synology_drive_api")
SYNOLOGY_PATH_KEY = AppKey[SynologyPathService]("synology_path")
MOUNT_REGISTRY_KEY = AppKey[MountRegistry]("mount_registry")
WEBHOOK_QUEUE_KEY = AppKey[WebhookQueue]("webhook_queue")
