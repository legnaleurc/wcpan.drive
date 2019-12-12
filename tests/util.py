from typing import List
import datetime

from wcpan.drive.types import NodeDict, ChangeDict


class ChangeListBuilder(object):

    def __init__(self):
        self._changes = []

    def update(self, dict_: NodeDict) -> None:
        self._changes.append({
            'removed': False,
            'node': dict_,
        })

    def delete(self, id_: str) -> None:
        self._changes.append({
            'removed': True,
            'id': id_,
        })

    def reset(self) -> None:
        self._changes = []

    @property
    def changes(self) -> List[ChangeDict]:
        return self._changes


def get_utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def create_root():
    return {
        'id': '__ROOT_ID__',
        'name': None,
        'is_folder': True,
        'trashed': False,
        'created': get_utc_now().isoformat(),
        'modified': get_utc_now().isoformat(),
        'parent_list': [],
        'size': None,
        'mime_type': None,
        'hash': None,
        'image': None,
        'video': None,
        'private': None,
    }


def create_folder(id_, name, parent_id):
    return {
        'id': id_,
        'name': name,
        'is_folder': True,
        'trashed': False,
        'created': get_utc_now().isoformat(),
        'modified': get_utc_now().isoformat(),
        'parent_list': [parent_id],
        'size': None,
        'mime_type': None,
        'hash': None,
        'image': None,
        'video': None,
        'private': None,
    }


def create_file(id_, name, parent_id, size, hash_, mime_type):
    return {
        'id': id_,
        'name': name,
        'is_folder': False,
        'trashed': False,
        'created': get_utc_now().isoformat(),
        'modified': get_utc_now().isoformat(),
        'parent_list': [parent_id],
        'size': size,
        'mime_type': mime_type,
        'hash': hash_,
        'image': None,
        'video': None,
        'private': None,
    }


def create_image(id_, name, parent_id, size, hash_, mime_type, width, height):
    return {
        'id': id_,
        'name': name,
        'is_folder': False,
        'trashed': False,
        'created': get_utc_now().isoformat(),
        'modified': get_utc_now().isoformat(),
        'parent_list': [parent_id],
        'size': size,
        'mime_type': mime_type,
        'hash': hash_,
        'image': {
            'width': width,
            'height': height,
        },
        'video': None,
        'private': None,
    }


def create_video(id_, name, parent_id, size, hash_, mime_type, width, height, ms_duration):
    return {
        'id': id_,
        'name': name,
        'is_folder': False,
        'trashed': False,
        'created': get_utc_now().isoformat(),
        'modified': get_utc_now().isoformat(),
        'parent_list': [parent_id],
        'size': size,
        'mime_type': mime_type,
        'hash': hash_,
        'image': None,
        'video': {
            'width': width,
            'height': height,
            'ms_duration': ms_duration,
        },
        'private': None,
    }

def toggle_trashed(dict_):
    dict_['trashed'] = not dict_['trashed']
    return dict_
