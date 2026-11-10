from unittest.mock import patch

from integrations.desktop_control.session import desktop_session_status
from integrations.desktop_control import server


def test_disconnected_desktop_is_explicitly_unavailable():
    with patch("integrations.desktop_control.session.os.name", "nt"), \
         patch("integrations.desktop_control.session._wts_connection_state", return_value=4):
        status = desktop_session_status()
    assert status["state"] == "disconnected"
    assert not status["available"]


def test_locked_desktop_is_explicitly_unavailable():
    with patch("integrations.desktop_control.session.os.name", "nt"), \
         patch("integrations.desktop_control.session._wts_connection_state", return_value=0), \
         patch("integrations.desktop_control.session._input_desktop_name", return_value="Winlogon"):
        status = desktop_session_status()
    assert status["state"] == "locked"


def test_desktop_tool_stops_before_touching_windows_when_disconnected():
    with patch.object(server, "desktop_session_status", return_value={
        "available": False, "state": "disconnected", "reason": "远程桌面已断开"
    }), patch.object(server, "_windows") as windows:
        try:
            server.call_tool("desktop_list_windows", {})
        except RuntimeError as error:
            assert "断开" in str(error)
        else:
            assert False, "must refuse disconnected UI access"
        windows.assert_not_called()
