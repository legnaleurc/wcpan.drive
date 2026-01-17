import json
from datetime import UTC, datetime
from sqlite3 import Cursor

from wcpan.drive.core.types import Node

from ._sql import JoinedDict


def inner_set_metadata(query: Cursor, key: str, value: str) -> None:
    query.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?);", (key, value))


def inner_get_metadata(query: Cursor, key: str) -> str | None:
    query.execute("SELECT value FROM metadata WHERE key = ?;", (key,))
    rv = query.fetchone()
    if not rv:
        return None
    return rv["value"]


def inner_get_node_by_id(query: Cursor, node_id: str) -> Node | None:
    from ._sql import SQL_SELECT_NODE_BY_ID

    query.execute(SQL_SELECT_NODE_BY_ID, (node_id,))
    rv = query.fetchone()
    if not rv:
        return None
    return node_from_query(rv)


def inner_insert_node(query: Cursor, node: Node) -> None:
    # add this node
    query.execute(
        "INSERT OR REPLACE INTO nodes "
        "(id, name, trashed, created, updated) "
        "VALUES "
        "(?, ?, ?, ?, ?);",
        (
            node.id,
            node.name,
            node.is_trashed,
            int(node.ctime.timestamp() * 1_000_000),
            int(node.mtime.timestamp() * 1_000_000),
        ),
    )

    # add file information
    if not node.is_directory:
        query.execute(
            "INSERT OR REPLACE INTO files "
            "(id, mime_type, hash, size) "
            "VALUES "
            "(?, ?, ?, ?);",
            (node.id, node.mime_type, node.hash, node.size),
        )

    # remove old parentage
    query.execute("DELETE FROM parents WHERE id=?;", (node.id,))
    # add parentage if there is any
    if node.parent_id:
        query.execute(
            "INSERT INTO parents (id, parent_id) VALUES (?, ?);",
            (node.id, node.parent_id),
        )

    # add image information
    if node.is_image or node.is_video:
        query.execute(
            "INSERT OR REPLACE INTO images (id, width, height) VALUES (?, ?, ?);",
            (node.id, node.width, node.height),
        )

    # add audio information
    if node.is_video:
        query.execute(
            "INSERT OR REPLACE INTO audios (id, ms_duration) VALUES (?, ?);",
            (node.id, node.ms_duration),
        )

    # add extra information
    if node.private:
        extra = json.dumps(node.private, separators=(",", ":"))
        query.execute(
            "INSERT OR REPLACE INTO extras (id, json) VALUES (?, ?);",
            (node.id, extra),
        )


def inner_delete_node_by_id(query: Cursor, node_id: str) -> None:
    # remove from extras
    query.execute("DELETE FROM extras WHERE id=?;", (node_id,))

    # remove from audios
    query.execute("DELETE FROM audios WHERE id=?;", (node_id,))

    # remove from images
    query.execute("DELETE FROM images WHERE id=?;", (node_id,))

    # disconnect parents
    query.execute("DELETE FROM parents WHERE id=? OR parent_id=?;", (node_id, node_id))

    # remove from files
    query.execute("DELETE FROM files WHERE id=?;", (node_id,))

    # remove from nodes
    query.execute("DELETE FROM nodes WHERE id=?;", (node_id,))


def node_from_query(row: JoinedDict) -> Node:
    mime_type = row["mime_type"]
    hash_ = row["hash"]
    size = row["size"]
    width = row["width"]
    height = row["height"]
    ms_duration = row["ms_duration"]
    extra = row["extra"]

    is_directory = any(_ is None for _ in (mime_type, hash_, size))
    has_image = all(_ is not None for _ in (width, height))
    has_audio = ms_duration is not None
    private = json.loads(extra) if extra else None
    return Node(
        id=row["id"],
        name=row["name"],
        is_trashed=bool(row["trashed"]),
        ctime=datetime.fromtimestamp(row["created"] / 1_000_000, UTC),
        mtime=datetime.fromtimestamp(row["updated"] / 1_000_000, UTC),
        parent_id=row["parent_id"],
        mime_type=mime_type or "",
        hash=hash_ or "",
        size=size or 0,
        width=width or 0,
        height=height or 0,
        ms_duration=ms_duration or 0,
        private=private,
        is_directory=is_directory,
        is_image=has_image and not has_audio,
        is_video=has_image and has_audio,
    )
