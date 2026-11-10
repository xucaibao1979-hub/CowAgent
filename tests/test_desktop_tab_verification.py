import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from integrations.desktop_control import server


class DesktopTabVerificationTests(unittest.TestCase):
    def test_tab_switch_requires_selected_state(self):
        tab = Mock()
        tab.window_text.return_value = "阿里云"
        tab.iface_selection_item.CurrentIsSelected = False
        window = Mock(handle=123)
        window.window_text.return_value = "其他页面"
        desktop = Mock()
        desktop.window.return_value.descendants.return_value = [tab]
        with patch.object(server, "_browser_window", return_value=(window, {})), \
             patch.object(server, "_desktop", return_value=desktop), \
             patch.object(server, "_capture") as capture, \
             patch.object(server.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "未确认切换"):
                server._browser_select_tab("chrome", 123, 0)
        capture.assert_not_called()

    def test_verified_tab_switch_returns_receipt(self):
        tab = Mock()
        tab.window_text.return_value = "阿里云"
        tab.iface_selection_item.CurrentIsSelected = True
        window = Mock(handle=123)
        desktop = Mock()
        desktop.window.return_value.descendants.return_value = [tab]
        with patch.object(server, "_browser_window", return_value=(window, {})), \
             patch.object(server, "_desktop", return_value=desktop), \
             patch.object(server, "_capture", return_value={"path": "test.png"}), \
             patch.object(server.time, "sleep"):
            result = server._browser_select_tab("chrome", 123, 0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["title"], "阿里云")


if __name__ == "__main__":
    unittest.main()
