from unittest import TestCase

from wcpan.drive.core._lib import is_valid_name


class TestUtilities(TestCase):
    def testIsValidName(self):
        ok = is_valid_name("name")
        self.assertTrue(ok)

        ok = is_valid_name("name/name")
        self.assertFalse(ok)

        ok = is_valid_name("./name")
        self.assertFalse(ok)

        ok = is_valid_name("name\\/name")
        self.assertFalse(ok)

        ok = is_valid_name("name\\name")
        self.assertFalse(ok)
