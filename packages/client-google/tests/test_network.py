from unittest.mock import patch

from aiohttp import ClientResponseError
from aiohttp.test_utils import AioHTTPTestCase
from aiohttp.web import (
    AppKey,
    Application,
    HTTPRequestTimeout,
    HTTPServiceUnavailable,
    HTTPUnauthorized,
    Request,
    Response,
    json_response,
)
from decoy import Decoy, matchers
from wcpan.drive.client.google._network import (
    BackoffController,
    QueryDict,
    create_network,
)
from wcpan.drive.client.google._oauth import OAuth2Manager
from wcpan.drive.client.google.exceptions import (
    DownloadAbusiveFileError,
    InvalidAbuseFlagError,
)


class NetworkTestCase(AioHTTPTestCase):
    async def asyncSetUp(self) -> None:
        from logging import CRITICAL, disable

        await super().asyncSetUp()

        disable(CRITICAL)

        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)
        self._oauth = self.decoy.mock(cls=OAuth2Manager)
        self.decoy.when(await self._oauth.safe_get_access_token()).then_return(
            "accepted"
        )
        with patch(
            "wcpan.drive.client.google._network.BackoffController"
        ) as FakeBackoffController:
            self._backoff = self.decoy.mock(cls=BackoffController)
            FakeBackoffController.return_value = self._backoff
            self._network = await self.enterAsyncContext(create_network(self._oauth))

    async def asyncTearDown(self) -> None:
        from logging import NOTSET, disable

        await super().asyncTearDown()

        disable(NOTSET)

    async def get_application(self) -> Application:
        return _setup_server()

    async def test200(self):
        url = str(self.server.make_url("/api/v1/200"))
        async with self._network.fetch("GET", url) as response:
            rv = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(rv, 42)

    async def test408(self):
        url = str(self.server.make_url("/api/v1/408"))
        with self.assertRaises(ClientResponseError):
            async with self._network.fetch("GET", url):
                pass

    async def test503(self):
        url = str(self.server.make_url("/api/v1/503"))
        async with self._network.fetch("GET", url) as response:
            rv = await response.json()
            self.assertEqual(rv, 42)
            self.decoy.verify(await self._backoff.wait())
            self.decoy.verify(self._backoff.increase(), times=1)
            self.decoy.verify(self._backoff.decrease(), times=1)

    async def testRefreshToken(self):
        self.decoy.when(await self._oauth.safe_get_access_token()).then_return(
            "expired",
            "accepted",
        )
        url = str(self.server.make_url("/api/v1/token"))
        async with self._network.fetch("GET", url) as response:
            rv = await response.json()
            self.decoy.verify(
                await self._oauth.renew_token(matchers.Anything()), times=1
            )
            self.assertEqual(rv, 42)

    async def testDownloadAbuse(self):
        url = str(self.server.make_url("/api/v1/cannotDownloadAbusiveFile"))
        with self.assertRaises(DownloadAbusiveFileError):
            async with self._network.fetch("GET", url):
                pass

    async def testInvalidAbuse(self):
        url = str(self.server.make_url("/api/v1/invalidAbuseAcknowledgment"))
        with self.assertRaises(InvalidAbuseFlagError):
            async with self._network.fetch("GET", url):
                pass

    async def testUsageLimits(self):
        url = str(self.server.make_url("/api/v1/usageLimits"))
        async with self._network.fetch("GET", url) as response:
            rv = await response.json()
            self.assertEqual(rv, 42)
            self.decoy.verify(await self._backoff.wait())
            self.decoy.verify(self._backoff.increase(), times=1)
            self.decoy.verify(self._backoff.decrease(), times=1)

    async def testQuery(self):
        url = str(self.server.make_url("/api/v1/query"))
        query: QueryDict = {
            "name": "__NAME__",
        }
        async with self._network.fetch("GET", url, query=query) as response:
            rv = await response.json()
            self.assertEqual(rv, "__NAME__")

    async def testHeaders(self):
        url = str(self.server.make_url("/api/v1/headers"))
        headers: dict[str, str] = {
            "X-Name": "__NAME__",
        }
        async with self._network.fetch("GET", url, headers=headers) as response:
            rv = await response.json()
            self.assertEqual(rv, "__NAME__")

    async def testBody(self):
        url = str(self.server.make_url("/api/v1/stream"))
        body = "__NAME__".encode("utf-8")
        async with self._network.fetch("POST", url, body=body) as response:
            rv = await response.json()
            self.assertEqual(rv, "__NAME__")

    async def testStream(self):
        url = str(self.server.make_url("/api/v1/stream"))
        body = "__NAME__".encode("utf-8")

        async def produce():
            yield body

        async with self._network.fetch("POST", url, body=produce()) as response:
            rv = await response.json()
            self.assertEqual(rv, "__NAME__")


_KEY_QUOTA = AppKey("quota", list[int])


def _setup_server() -> Application:
    app = Application()
    app[_KEY_QUOTA] = [0]
    app.router.add_get("/api/v1/200", _on_200)
    app.router.add_get("/api/v1/408", _on_408)
    app.router.add_get("/api/v1/503", _on_503)
    app.router.add_get("/api/v1/token", _on_token)
    app.router.add_get("/api/v1/cannotDownloadAbusiveFile", _on_download_abuse)
    app.router.add_get("/api/v1/invalidAbuseAcknowledgment", _on_invalid_abuse)
    app.router.add_get("/api/v1/usageLimits", _on_useage_limits)
    app.router.add_get("/api/v1/query", _on_query)
    app.router.add_get("/api/v1/headers", _on_headers)
    app.router.add_post("/api/v1/stream", _on_stream)
    return app


async def _on_200(request: Request) -> Response:
    return json_response(42)


async def _on_408(request: Request) -> Response:
    raise HTTPRequestTimeout


async def _on_503(request: Request) -> Response:
    quota = request.app[_KEY_QUOTA]
    if quota:
        quota.pop()
        raise HTTPServiceUnavailable
    return json_response(42)


async def _on_token(request: Request) -> Response:
    auth = request.headers.getone("Authorization")
    if auth != "Bearer accepted":
        raise HTTPUnauthorized
    return json_response(42)


async def _on_download_abuse(request: Request) -> Response:
    return json_response(
        {
            "error": {
                "errors": [
                    {
                        "domain": "",
                        "reason": "cannotDownloadAbusiveFile",
                        "message": "error",
                    },
                ],
            },
        },
        status=403,
    )


async def _on_invalid_abuse(request: Request) -> Response:
    return json_response(
        {
            "error": {
                "errors": [
                    {
                        "domain": "",
                        "reason": "invalidAbuseAcknowledgment",
                        "message": "error",
                    },
                ],
            },
        },
        status=403,
    )


async def _on_useage_limits(request: Request) -> Response:
    quota = request.app[_KEY_QUOTA]
    if quota:
        quota.pop()
        return json_response(
            {
                "error": {
                    "errors": [
                        {
                            "domain": "usageLimits",
                            "reason": "",
                            "message": "",
                        },
                    ],
                },
            },
            status=403,
        )
    return json_response(42)


async def _on_query(request: Request) -> Response:
    name = request.query["name"]
    return json_response(name)


async def _on_headers(request: Request) -> Response:
    name = request.headers["X-Name"]
    return json_response(name)


async def _on_stream(request: Request) -> Response:
    body = await request.content.read()
    return json_response(body.decode("utf-8"))
