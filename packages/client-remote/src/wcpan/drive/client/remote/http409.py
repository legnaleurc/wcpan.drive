"""Parse HTTP 409 bodies from the wcpan.drive.client.remote server."""

from typing import Any, cast

from aiohttp import ClientResponse
from wcpan.drive.client.types import Node

from ._lib import NodeRecordDict, node_from_record, node_record_from_dict


async def node_from_409(response: ClientResponse) -> Node | None:
    """If the JSON body is a node record (has ``id``), return a ``Node``."""
    try:
        body: Any = await response.json()
    except Exception:
        return None
    if not isinstance(body, dict) or "id" not in body:
        return None
    return node_from_record(node_record_from_dict(cast(NodeRecordDict, body)))
