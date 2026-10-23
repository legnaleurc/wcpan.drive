from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any
from unittest import IsolatedAsyncioTestCase

from aiohttp import ClientResponse
from decoy import Decoy, Stub
from multidict import CIMultiDict, CIMultiDictProxy


class DecoyTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)

    async def create_response(
        self,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> ClientResponse:
        response = self.decoy.mock(cls=ClientResponse)
        self.decoy.when(response.status).then_return(status)
        self.decoy.when(response.headers).then_return(
            CIMultiDictProxy(CIMultiDict(headers or {}))
        )
        self.decoy.when(await response.json()).then_return(body)
        return response

    def when_enter(
        self,
        request: AbstractAsyncContextManager[ClientResponse],
        *responses: ClientResponse,
    ) -> None:
        """Make the rehearsed request enter each response in turn; the last repeats.

        Create the responses first: rehearsing a request must be the last mock
        call before this.
        """
        stub: Stub[AbstractAsyncContextManager[ClientResponse]] = self.decoy.when(
            request
        )
        stub.then_return(*(nullcontext(_) for _ in responses))
