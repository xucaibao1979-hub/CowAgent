import unittest
from copy import deepcopy

from intelligence_center.inbox_service import InboxStore


class InboxStoreTest(unittest.TestCase):
    def test_upsert_sort_filter_and_mark_read(self):
        store = InboxStore(".")
        state = {"version": 1, "items": []}
        store._read = lambda: deepcopy(state)
        store._write = lambda data: (state.clear(), state.update(deepcopy(data)))

        first = store.upsert(source_id="approval-1", kind="approval", title="请求审批")
        store.upsert(source_id="run-1", kind="automation_result", title="任务完成")
        updated = store.upsert(source_id="approval-1", kind="approval", title="审批已执行", status="success")

        self.assertEqual(first["id"], updated["id"])
        self.assertEqual(store.list()["total"], 2)
        self.assertEqual(store.list(unread_only=True)["unread"], 2)
        self.assertEqual(store.mark(first["id"], unread=False), 1)
        self.assertEqual(store.list()["unread"], 1)
        self.assertEqual(store.mark(all_items=True, unread=False), 2)
        self.assertEqual(store.list()["unread"], 0)


if __name__ == "__main__":
    unittest.main()
