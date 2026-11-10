import tempfile
import unittest

from intelligence_center.fact_store import FactStore
from intelligence_center.auto_memory import observe_user_message


class FactCorrectionTests(unittest.TestCase):
    def test_correction_supersedes_old_fact_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactStore(root)
            old = store.upsert(content="通知默认发送到旧邮箱", fact_type="notification_preference",
                               source="用户消息", source_url="https://example.com/source", confidence=0.8)
            new = store.correct(old["id"], "通知默认发送到新邮箱")
            self.assertEqual(store.get(old["id"])["status"], "disabled")
            self.assertEqual(new["status"], "active")
            self.assertEqual(new["confidence"], 1.0)
            self.assertEqual(new["metadata"]["supersedes"], old["id"])
            self.assertEqual(new["source_url"], old["source_url"])
            self.assertEqual([x["id"] for x in store.search("通知默认")], [new["id"]])

    def test_same_content_does_not_disable_old_fact(self):
        with tempfile.TemporaryDirectory() as root:
            store = FactStore(root)
            old = store.upsert(content="偏好简短回答")
            with self.assertRaises(ValueError):
                store.correct(old["id"], "偏好简短回答")
            self.assertEqual(store.get(old["id"])["status"], "active")

    def test_similar_new_preference_waits_for_review(self):
        with tempfile.TemporaryDirectory() as root:
            first = observe_user_message(root, "以后通知默认发送到微信")[0]
            second = observe_user_message(root, "以后通知默认发送到邮箱")[0]
            store = FactStore(root)
            self.assertEqual(store.get(first["id"])["status"], "active")
            self.assertEqual(store.get(second["id"])["status"], "pending")
            self.assertIn(first["id"], store.get(second["id"])["metadata"]["possible_conflicts"])


if __name__ == "__main__":
    unittest.main()
