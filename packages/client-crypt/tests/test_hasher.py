from wcpan.drive.client.crypt._lib import EncryptHasher, encrypt
from wcpan.drive.client.types import Hasher

from ._lib import DecoyTestCase


class HasherTestCase(DecoyTestCase):
    def setUp(self):
        super().setUp()
        self._mock = self.decoy.mock(cls=Hasher)
        self._hasher = EncryptHasher(self._mock)

    async def testUpdate(self):
        chunk = b"1234abcd"
        await self._hasher.update(chunk)
        self.decoy.verify(await self._mock.update(encrypt(chunk)))

    async def testDigest(self):
        self.decoy.when(await self._mock.digest()).then_return(b"digest")
        self.assertEqual(await self._hasher.digest(), b"digest")

    async def testHexdigest(self):
        self.decoy.when(await self._mock.hexdigest()).then_return("hexdigest")
        self.assertEqual(await self._hasher.hexdigest(), "hexdigest")

    async def testCopy(self):
        self.decoy.when(await self._mock.copy()).then_return(
            self.decoy.mock(cls=Hasher)
        )
        clone = await self._hasher.copy()
        self.assertIsInstance(clone, EncryptHasher)
