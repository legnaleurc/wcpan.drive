"""Backend instance registration and each instance's binding to its backend tables."""

import os
import sqlite3
from contextlib import closing

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.sqlite import (
    read_only as _read_only,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from .model import (
    BackendNamespace,
    BackendSchema,
    MirrorNotReadyError,
    SchemaVersionError,
)


def register_instance(dsn: str, name: str) -> int:
    """The registry key of instance *name*, registering it on first sight."""
    with _read_write(dsn) as con:
        with closing(
            con.execute("SELECT key FROM backend_instances WHERE name = ?", (name,))
        ) as cur:
            row = cur.fetchone()
        if row is not None:
            return int(row["key"])
        cur = con.execute("INSERT INTO backend_instances (name) VALUES (?)", (name,))
        return int(cur.lastrowid or 0)


def lookup_instance(dsn: str, name: str) -> int | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute("SELECT key FROM backend_instances WHERE name = ?", (name,))
        ) as cur:
            row = cur.fetchone()
    return int(row["key"]) if row is not None else None


def register_schema(
    dsn: str, name: str, ns: BackendNamespace, schema: BackendSchema
) -> None:
    """Create a backend's tables, bind instance *name* to them, and drop its
    rows whose nodes went away meanwhile.

    Offline ``gc`` runs without loading backends, so it cannot clean their rows.

    Raises:
        SchemaVersionError: If the backend's tables have another version, or
            *name* holds rows of another backend's tables.
    """
    with _read_write(dsn) as con:
        with closing(
            con.execute("SELECT schema FROM backend_instances WHERE name = ?", (name,))
        ) as cur:
            bound = cur.fetchone()["schema"]
        if bound is not None and bound != schema.name:
            raise SchemaVersionError(
                f"backend {name!r} holds rows of backend schema {bound!r},"
                f" not {schema.name!r}; drop it to change its backend"
            )
        with closing(
            con.execute(
                "SELECT version FROM backend_schemas WHERE name = ?", (schema.name,)
            )
        ) as cur:
            row = cur.fetchone()
        if row is not None and int(row["version"]) != schema.version:
            raise SchemaVersionError(
                f"backend schema {schema.name!r} version mismatch: expected"
                f" {schema.version}, got {row['version']}. Its tables are not"
                " migrated: move the database aside to rebuild it."
            )
        schema.ensure_schema(con)
        if row is None:
            con.execute(
                "INSERT INTO backend_schemas (name, version) VALUES (?, ?)",
                (schema.name, schema.version),
            )
        if bound is None:
            con.execute(
                "UPDATE backend_instances SET schema = ? WHERE name = ?",
                (schema.name, name),
            )
        schema.forget_orphans(con, ns)


def check_backend_schema(dsn: str, name: str, schema: BackendSchema) -> int:
    """The key of instance *name*, checked against *schema* without changes.

    Raises:
        MirrorNotReadyError: If the server has not registered the instance or
            bound it to its backend tables yet.
        SchemaVersionError: If the instance holds another backend's rows, or
            the backend's tables have another version.
    """
    start = "start the server once to set it up"
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                "SELECT key, schema FROM backend_instances WHERE name = ?", (name,)
            )
        ) as cur:
            instance = cur.fetchone()
        if instance is None:
            raise MirrorNotReadyError(f"backend {name!r} is not registered; {start}")
        bound = instance["schema"]
        if bound is None:
            raise MirrorNotReadyError(
                f"backend {name!r} has no backend tables yet; {start}"
            )
        if bound != schema.name:
            raise SchemaVersionError(
                f"backend {name!r} holds rows of backend schema {bound!r},"
                f" not {schema.name!r}; drop it to change its backend"
            )
        with closing(
            con.execute(
                "SELECT version FROM backend_schemas WHERE name = ?", (schema.name,)
            )
        ) as cur:
            row = cur.fetchone()
        if row is None:
            raise MirrorNotReadyError(
                f"backend schema {schema.name!r} is not set up; {start}"
            )
        if int(row["version"]) != schema.version:
            raise SchemaVersionError(
                f"backend schema {schema.name!r} version mismatch: expected"
                f" {schema.version}, got {row['version']}. Its tables are not"
                " migrated: move the database aside to rebuild it."
            )
        return int(instance["key"])


def owner_of(dsn: str, node_id: NodeId) -> str | None:
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                """
                SELECT b.name FROM node_ids i JOIN backend_instances b ON b.key = i.instance
                WHERE i.id = ?
                """,
                (str(node_id),),
            )
        ) as cur:
            row = cur.fetchone()
    return str(row["name"]) if row is not None else None


def registered_instances(dsn: str) -> list[str]:
    """Names of the instances registered in database *dsn*; none if it is new.

    Reads without creating or checking the schema, so command-line parsing can
    offer instances that are no longer configured.
    """
    if not os.path.exists(dsn):
        return []
    try:
        with _read_only(dsn) as con:
            with closing(
                con.execute("SELECT name FROM backend_instances ORDER BY name")
            ) as cur:
                return [str(row["name"]) for row in cur]
    except sqlite3.Error:
        return []


def instance_key(con: sqlite3.Connection, name: str) -> int:
    with closing(
        con.execute("SELECT key FROM backend_instances WHERE name = ?", (name,))
    ) as cur:
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"backend {name!r} is not registered in this database")
    return int(row["key"])
