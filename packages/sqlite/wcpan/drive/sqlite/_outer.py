from datetime import datetime
from pathlib import PurePath
from typing import cast

from wcpan.drive.core.lib import dispatch_change
from wcpan.drive.core.types import ChangeAction, Node

from ._inner import (
    inner_delete_node_by_id,
    inner_get_metadata,
    inner_get_node_by_id,
    inner_insert_node,
    inner_set_metadata,
    node_from_query,
)
from ._lib import read_only, read_write, sqlite3_regexp
from .exceptions import SqliteSnapshotError


KEY_ROOT_ID = "root_id"
KEY_CURSOR = "check_point"


def initialize(dsn: str, /):
    from ._sql import CURRENT_SCHEMA_VERSION, SQL_CREATE_TABLES

    with read_write(dsn) as query:
        # check the schema version
        query.execute("PRAGMA user_version;")
        rv = query.fetchone()
        if not rv:
            raise SqliteSnapshotError("no user_version")
        version = int(rv[0])

        if version != 0 and version != CURRENT_SCHEMA_VERSION:
            raise SqliteSnapshotError(
                "schema has been changed, please rebuild snapshot"
            )

        # initialize table
        for sql in SQL_CREATE_TABLES:
            query.execute(sql)


def get_node_by_path(dsn: str, path: PurePath, /) -> Node | None:
    # the first part is "/"
    parts = path.parts[1:]
    with read_only(dsn) as query:
        node_id = inner_get_metadata(query, "root_id")
        if not node_id:
            return None

        for part in parts:
            query.execute(
                "SELECT nodes.id AS id "
                "FROM parents "
                "INNER JOIN nodes ON parents.id = nodes.id "
                "WHERE parents.parent_id=? AND nodes.name=?;",
                (node_id, part),
            )
            rv = query.fetchone()
            if not rv:
                return None
            node_id = cast(str, rv["id"])

        node = inner_get_node_by_id(query, node_id)
    return node


def resolve_path_by_id(dsn: str, node_id: str, /) -> PurePath | None:
    parts: list[str] = []
    with read_only(dsn) as query:
        while True:
            query.execute("SELECT name FROM nodes WHERE id=?;", (node_id,))
            rv = query.fetchone()
            if not rv:
                return None

            name = rv["name"]

            query.execute("SELECT parent_id FROM parents WHERE id=?;", (node_id,))
            rv = query.fetchone()
            if not rv:
                # reached root
                parts.insert(0, "/")
                break

            parts.insert(0, name)
            node_id = rv["parent_id"]

    path = PurePath(*parts)
    return path


def get_child_by_name(dsn: str, name: str, parent_id: str, /) -> Node | None:
    from ._sql import SQL_SELECT_CHILD_BY_NAME

    with read_only(dsn) as query:
        query.execute(SQL_SELECT_CHILD_BY_NAME, (parent_id, name))
        rv = query.fetchone()
        if not rv:
            return None
    return node_from_query(rv)


def get_children_by_id(dsn: str, node_id: str, /) -> list[Node]:
    from ._sql import SQL_SELECT_CHILDREN_BY_ID

    with read_only(dsn) as query:
        query.execute(SQL_SELECT_CHILDREN_BY_ID, (node_id,))
        nodes = [node_from_query(_) for _ in query]
    return nodes


def get_trashed_nodes(dsn: str, /) -> list[Node]:
    from ._sql import SQL_SELECT_TRASHED_NODES

    with read_only(dsn) as query:
        query.execute(SQL_SELECT_TRASHED_NODES, (True,))
        nodes = [node_from_query(_) for _ in query]
    return nodes


def apply_changes(dsn: str, changes: list[ChangeAction], cursor: str, /) -> None:
    with read_write(dsn) as query:
        for change in changes:
            dispatch_change(
                change,
                on_remove=lambda _: inner_delete_node_by_id(query, _),
                on_update=lambda _: inner_insert_node(query, _),
            )
        inner_set_metadata(query, KEY_CURSOR, cursor)


def find_nodes_by_regex(dsn: str, pattern: str, /) -> list[Node]:
    from functools import partial
    from re import I, compile

    from ._sql import SQL_SELECT_NODES_BY_REGEX

    fn = partial(sqlite3_regexp, pattern=compile(pattern, I))
    with read_only(dsn, regexp=fn) as query:
        query.execute(SQL_SELECT_NODES_BY_REGEX)
        rv = [node_from_query(_) for _ in query]
    return rv


def get_current_cursor(dsn: str, /) -> str | None:
    with read_only(dsn) as query:
        return inner_get_metadata(query, KEY_CURSOR)


def get_root(dsn: str, /) -> Node | None:
    with read_only(dsn) as query:
        root_id = inner_get_metadata(query, KEY_ROOT_ID)
        if not root_id:
            return None
        return inner_get_node_by_id(query, root_id)


def set_root(dsn: str, root: Node, /) -> None:
    with read_write(dsn) as query:
        inner_set_metadata(query, KEY_ROOT_ID, root.id)
        inner_insert_node(query, root)


def get_node_by_id(dsn: str, node_id: str, /) -> Node | None:
    with read_only(dsn) as query:
        return inner_get_node_by_id(query, node_id)


def get_uploaded_size(dsn: str, begin: datetime, end: datetime) -> int:
    b = int(begin.timestamp() * 1_000_000)
    e = int(end.timestamp() * 1_000_000)
    with read_only(dsn) as query:
        query.execute(
            "SELECT SUM(size) AS sum "
            "FROM files "
            "INNER JOIN nodes ON files.id = nodes.id "
            "WHERE created >= ? AND created < ?;",
            (b, e),
        )
        rv = query.fetchone()
        if not rv:
            return 0
        if rv["sum"] is None:
            return 0
        return rv["sum"]


def find_orphan_nodes(dsn: str) -> list[Node]:
    from ._sql import SQL_SELECT_ORPHAN_NODES

    with read_only(dsn) as query:
        query.execute(SQL_SELECT_ORPHAN_NODES)
        nodes = [node_from_query(_) for _ in query]
    return nodes


def find_multiple_parents_nodes(dsn: str) -> list[Node]:
    with read_only(dsn) as query:
        query.execute(
            "SELECT id, COUNT(id) AS parent_count "
            "FROM parents "
            "GROUP BY id "
            "HAVING parent_count > 1;"
        )
        rv = query.fetchall()
        raw_query = (inner_get_node_by_id(query, _["id"]) for _ in rv)
        nodes = [_ for _ in raw_query if _]
    return nodes
