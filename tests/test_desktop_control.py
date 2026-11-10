import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from integrations.desktop_control.policy import read_policy, write_policy


ROOT = Path(__file__).resolve().parents[1]


class DesktopControlPolicyTests(unittest.TestCase):
    def test_policy_defaults_closed_and_normalizes_apps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "desktop.json"
            self.assertFalse(read_policy(path)["any_app"])
            self.assertFalse(any(read_policy(path)["apps"].values()))
            write_policy({"apps": {"wecom": True, "unknown": True}}, path)
            saved = read_policy(path)
            self.assertTrue(saved["apps"]["wecom"])
            self.assertNotIn("unknown", saved["apps"])

    def test_mcp_handshake_and_disabled_app_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "desktop.json"
            write_policy({"apps": {"wecom": True}}, path)
            env = {**os.environ, "COW_DESKTOP_CONTROL_CONFIG": str(path)}
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "integrations/desktop_control/server.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", text=True, env=env,
            )
            def cleanup():
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            self.addCleanup(cleanup)

            def request(identifier, method, params=None):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                                "method": method, "params": params or {}}) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            self.assertIn("result", request(1, "initialize"))
            names = [tool["name"] for tool in request(2, "tools/list")["result"]["tools"]]
            self.assertIn("desktop_open_app", names)
            self.assertIn("desktop_wecom_search_contact", names)
            self.assertIn("desktop_wecom_select_contact", names)
            self.assertIn("desktop_wecom_prepare_file", names)
            self.assertIn("desktop_wecom_send_staged_file", names)
            self.assertIn("desktop_wecom_prepare_text", names)
            self.assertIn("desktop_wecom_send_staged_text", names)
            premature_send = request(6, "tools/call", {
                "name": "desktop_wecom_send_staged_file",
                "arguments": {"confirmed_contact": "someone", "confirmed_file": "x.png",
                              "send_x": 10, "send_y": 10},
            })
            self.assertTrue(premature_send["result"]["isError"])
            missing_search = request(5, "tools/call", {
                "name": "desktop_wecom_select_contact",
                "arguments": {"contact": "someone", "x": 20, "y": 20},
            })
            self.assertTrue(missing_search["result"]["isError"])
            denied = request(3, "tools/call", {"name": "desktop_open_app",
                                               "arguments": {"app": "excel"}})
            self.assertTrue(denied["result"]["isError"])
            write_policy({}, path)
            denied_again = request(4, "tools/call", {"name": "desktop_open_app",
                                                     "arguments": {"app": "wecom"}})
            self.assertTrue(denied_again["result"]["isError"])
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
