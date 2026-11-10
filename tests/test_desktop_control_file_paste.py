import tempfile
import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from integrations.desktop_control.server import _paste_focused, _resolve_authorized_file, _wecom_prepare_text


class DesktopFilePasteTests(unittest.TestCase):
    def test_exact_desktop_filename_resolves_without_opening_content(self):
        with tempfile.TemporaryDirectory() as folder:
            desktop = Path(folder) / "Desktop"
            desktop.mkdir()
            target = desktop / "2.png"
            target.write_bytes(b"test")
            fake_index = type("Index", (), {
                "roots": lambda self: [desktop],
                "_desktop_path": lambda self: desktop,
                "search": lambda self, intent: self.fail("index scan was unnecessary"),
            })()
            with patch("common.local_file_index.LocalFileIndex", return_value=fake_index):
                self.assertEqual(_resolve_authorized_file("2.png", "桌面上"), target.resolve())

    def test_duplicate_filename_requires_explicit_path(self):
        with tempfile.TemporaryDirectory() as folder:
            roots = [Path(folder) / "one", Path(folder) / "two"]
            for root in roots:
                root.mkdir()
                (root / "report.txt").write_text("x", encoding="utf-8")
            fake_index = type("Index", (), {
                "roots": lambda self: roots,
                "_desktop_path": lambda self: roots[0],
            })()
            with patch("common.local_file_index.LocalFileIndex", return_value=fake_index):
                with self.assertRaisesRegex(ValueError, "多个同名文件"):
                    _resolve_authorized_file("report.txt")

    def test_file_paste_restores_previous_clipboard_text(self):
        data = {13: "previous clipboard"}
        def next_format(current):
            return next((fmt for fmt in sorted(data) if fmt > current), 0)
        fake_clipboard = SimpleNamespace(
            CF_UNICODETEXT=13, CF_TEXT=1, CF_DIB=8, CF_DIBV5=17, CF_HDROP=15,
            OpenClipboard=lambda: None, CloseClipboard=lambda: None,
            EnumClipboardFormats=next_format,
            GetClipboardFormatName=lambda fmt: "",
            GetClipboardData=lambda fmt: data[fmt],
            EmptyClipboard=lambda: data.clear(),
            SetClipboardData=lambda fmt, value: data.__setitem__(fmt, value),
        )
        def inspect_paste(keys):
            self.assertEqual(keys, "^v")
            self.assertIn(15, data)
            self.assertIn("2.png".encode("utf-16le"), data[15])
        with (patch.dict(sys.modules, {"win32clipboard": fake_clipboard}),
              patch("pywinauto.keyboard.send_keys", side_effect=inspect_paste),
              patch("integrations.desktop_control.server.time.sleep")):
            _paste_focused(r"C:\Users\Bob.Xu\Desktop\2.png", as_file=True)
        self.assertEqual(data, {13: "previous clipboard"})

    def test_existing_chat_draft_is_cleared_before_new_text(self):
        calls = []
        with (patch("integrations.desktop_control.server._selected_wecom_contact", "Alice"),
              patch("integrations.desktop_control.server._wecom_main", return_value=object()),
              patch("integrations.desktop_control.server._assert_selected_wecom_chat"),
              patch("integrations.desktop_control.server._wecom_input_point", return_value=(400, 900)),
              patch("pywinauto.mouse.click", side_effect=lambda **kwargs: calls.append("click")),
              patch("integrations.desktop_control.server._clear_wecom_editor", side_effect=lambda: calls.append("clear")),
              patch("integrations.desktop_control.server._paste_focused", side_effect=lambda text: calls.append("paste")),
              patch("integrations.desktop_control.server._capture", return_value={"path": "test.png"})):
            _wecom_prepare_text("hello", 400, 900)
        self.assertEqual(calls, ["click", "clear", "paste"])


if __name__ == "__main__":
    unittest.main()
