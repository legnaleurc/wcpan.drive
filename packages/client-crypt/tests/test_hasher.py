from wcpan.drive.client.crypt._lib import EncryptHasher, encrypt
from wcpan.drive.client.types import Hasher

from ._lib import DecoyTestCase


class HasherTestCase(DecoyTestCase):
    def setUp(self):
        super().setUp()
        self._mock = self.decoy.mock(cls=Hasher)
        self._hasher = EncryptHasher(self._mock)

    async def testUpdate(self):
        # given
        chunk = b"1234abcd"
        # when
        await self._hasher.update(chunk)
        # then
        self.decoy.verify(await self._mock.update(encrypt(chunk)))

    async def testDigest(self):
        # given
        self.decoy.when(await self._mock.digest()).then_return(b"digest")
        # when / then
        self.assertEqual(await self._hasher.digest(), b"digest")

    async def testHexdigest(self):
        # given
        self.decoy.when(await self._mock.hexdigest()).then_return("hexdigest")
        # when / then
        self.assertEqual(await self._hasher.hexdigest(), "hexdigest")

    async def testCopy(self):
        # given
        self.decoy.when(await self._mock.copy()).then_return(
            self.decoy.mock(cls=Hasher)
        )
        # when
        clone = await self._hasher.copy()
        # then
        self.assertIsInstance(clone, EncryptHasher)
