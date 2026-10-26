import unittest
import uuid
from pathlib import Path

from common.api_access import ApiKeyStore


class ApiAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("work/test-data") / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.store = ApiKeyStore(str(self.root / "api" / "access_keys.json"))

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_key_is_shown_once_and_stored_as_hash(self):
        created = self.store.create("自动化测试")
        self.assertTrue(created["token"].startswith("cow_"))
        self.assertNotIn("token", self.store.list_keys()[0])
        raw = (self.root / "api" / "access_keys.json").read_text(encoding="utf-8")
        self.assertNotIn(created["token"], raw)

    def test_authenticate_and_revoke(self):
        created = self.store.create("客户端")
        self.assertEqual(created["id"], self.store.authenticate(created["token"])["id"])
        self.assertIsNone(self.store.authenticate("cow_invalid"))
        self.store.revoke(created["id"])
        self.assertIsNone(self.store.authenticate(created["token"]))

    def test_duplicate_names_are_allowed_but_tokens_are_unique(self):
        first = self.store.create("服务")
        second = self.store.create("服务")
        self.assertNotEqual(first["token"], second["token"])


if __name__ == "__main__":
    unittest.main()
