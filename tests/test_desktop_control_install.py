import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.desktop_control.installer import _clean_icon_path, install_command


class DesktopControlInstallTests(unittest.TestCase):
    def test_only_fixed_app_ids_can_be_installed(self):
        with patch("integrations.desktop_control.installer.winget_path", return_value=r"C:\winget.exe"):
            command = install_command("wecom")
            self.assertEqual(command[1:5], ["install", "--id", "Tencent.WeCom", "--exact"])
            self.assertIn("--no-upgrade", command)
            with self.assertRaises(ValueError):
                install_command("some.other.package")
            with self.assertRaises(ValueError):
                install_command("excel")

    def test_quoted_display_icon_is_usable_for_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "app.exe"
            executable.touch()
            self.assertEqual(_clean_icon_path(f'"{executable}",0'), str(executable))

    def test_successful_install_enables_and_launches_app(self):
        from channel.web.web_channel import DesktopControlHandler
        with (patch("integrations.desktop_control.installer.install_app", return_value=(True, "ok")),
              patch("integrations.desktop_control.installer.installed_executable", return_value=r"C:\app.exe"),
              patch("integrations.desktop_control.installer.launch_app") as launch,
              patch.object(DesktopControlHandler, "_enable_app") as enable):
            DesktopControlHandler._install_worker("wecom")
        enable.assert_called_once_with("wecom")
        launch.assert_called_once_with("wecom")
        self.assertEqual(DesktopControlHandler._install_jobs["wecom"]["state"], "success")
        DesktopControlHandler._install_jobs.pop("wecom", None)


if __name__ == "__main__":
    unittest.main()
