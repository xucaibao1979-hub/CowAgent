"""Offline regression tests for DingTalk video message duration."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.reply import Reply, ReplyType
from channel.dingtalk.dingtalk_channel import DingTalkChanel


ChannelClass = next(cell.cell_contents for cell in DingTalkChanel.__closure__ if isinstance(cell.cell_contents, type))


class DingTalkVideoDurationTests(unittest.TestCase):
    def test_probe_rounds_up_to_whole_seconds(self):
        with patch("channel.dingtalk.dingtalk_channel.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="12.2\n")
            self.assertEqual(ChannelClass._get_video_duration("video.mp4"), 13)
            self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_probe_failures_keep_legacy_fallback(self):
        for result in ("0", "nan", "bad"):
            with self.subTest(result=result), patch("channel.dingtalk.dingtalk_channel.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=result)
                self.assertEqual(ChannelClass._get_video_duration("video.mp4"), 30)
        with patch("channel.dingtalk.dingtalk_channel.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(ChannelClass._get_video_duration("video.mp4"), 30)
        with patch("channel.dingtalk.dingtalk_channel.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 10)):
            self.assertEqual(ChannelClass._get_video_duration("video.mp4"), 30)

    def test_send_uses_local_duration_and_preserves_non_video(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "clip.mp4"
            video.write_bytes(b"placeholder")
            for filename, expected_key, expected_duration in (
                (f"file://{video}", "sampleVideo", "13"),
                (str(video), "sampleVideo", "13"),
                (str(Path(temp_dir) / "clip.txt"), "sampleFile", None),
                ("https://example.invalid/clip.mp4", "sampleVideo", "30"),
            ):
                with self.subTest(filename=filename):
                    channel = object.__new__(ChannelClass)
                    channel._robot_code = None
                    channel.get_access_token = lambda: "test-token"
                    channel.upload_media = lambda *_args, **_kwargs: "test-media"
                    sent = []
                    channel._send_file_message = lambda *_args: sent.append(_args) or True
                    class TestContext(dict):
                        kwargs = {"msg": SimpleNamespace(is_group=False, incoming_message="test")}
                    test_context = TestContext(receiver="test-receiver")
                    with patch.object(channel, "_get_video_duration", return_value=13) as probe:
                        channel.send(Reply(ReplyType.FILE, filename), test_context)
                    self.assertEqual(sent[0][2], expected_key)
                    if expected_duration is not None:
                        self.assertEqual(sent[0][3]["duration"], expected_duration)
                    self.assertEqual(probe.call_count, 1 if expected_duration == "13" else 0)


if __name__ == "__main__":
    unittest.main()
