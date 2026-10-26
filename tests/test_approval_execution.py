import unittest
import copy

from intelligence_center.approval_execution import ApprovalExecutionError, ApprovalExecutorRegistry
from plugins.intelligence_center.platform_features import ApprovalStore


class ApprovalExecutionTest(unittest.TestCase):
    def test_registered_actions_are_idempotent_and_unknown_actions_are_blocked(self):
        calls = []
        state = {"version": 1, "items": []}
        store = ApprovalStore(".")
        store._read = lambda: copy.deepcopy(state)
        store._write = lambda data: state.update(copy.deepcopy(data))
        registry = ApprovalExecutorRegistry().register(
            "demo", lambda preview, item: calls.append(item["id"]) or {"ok": True}
        )
        approval = store.create("task", "demo", {"value": 1})
        first = store.decide(approval["id"], "approved", registry.execute)
        second = store.decide(approval["id"], "approved", registry.execute)
        self.assertEqual("completed", first["execution_status"])
        self.assertEqual("completed", second["execution_status"])
        self.assertEqual(1, len(calls))

        unknown = store.create("task", "unknown", {})
        with self.assertRaises(ApprovalExecutionError):
            store.decide(unknown["id"], "approved", registry.execute)
        self.assertEqual("failed", store.get(unknown["id"])["execution_status"])


if __name__ == "__main__":
    unittest.main()
