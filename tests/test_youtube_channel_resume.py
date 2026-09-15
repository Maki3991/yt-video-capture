from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import youtube_asr  # noqa: E402
import youtube_channel_to_md  # noqa: E402


CHANNEL_URL = "https://www.youtube.com/@TestChannel/videos"
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class YouTubeChannelResumeTests(unittest.TestCase):
    def test_run_dir_reuses_task_without_caption_or_audio_download(self) -> None:
        with TemporaryDirectory() as temp:
            run_root = Path(temp)
            item_dir = run_root / "items" / "001-video-1"
            video_dir = item_dir / "video"
            video_dir.mkdir(parents=True)
            youtube_asr.write_json(
                run_root / "run.json",
                {
                    "schema_version": 1,
                    "source_url": CHANNEL_URL,
                    "items": [
                        {
                            "source_id": "video-1",
                            "source_url": VIDEO_URL,
                            "title": "测试视频",
                            "status": "partial",
                            "stages": youtube_asr.initial_stage_states(),
                        }
                    ],
                },
            )
            youtube_asr.write_json(
                item_dir / "metadata.json",
                {
                    "source_id": "video-1",
                    "source_url": VIDEO_URL,
                    "title": "测试视频",
                    "author": "测试频道",
                    "published_at": "2026-09-15T00:00:00Z",
                },
            )
            youtube_asr.write_json(
                video_dir / "request-info.json",
                {
                    "schema_version": 1,
                    "status": "submitted",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "source_url": VIDEO_URL,
                    "attempt": 1,
                    "task_id": "task-channel-existing",
                },
            )
            youtube_asr.write_json(
                video_dir / "submit.json",
                {"output": {"task_id": "task-channel-existing"}},
            )
            youtube_asr.write_json(
                video_dir / "task.json",
                {"output": {"task_status": "RUNNING"}},
            )

            def fake_transcribe(source_url: str, artifact_dir: Path, **kwargs):
                self.assertEqual(source_url, VIDEO_URL)
                self.assertEqual(artifact_dir, video_dir.resolve())
                self.assertTrue(kwargs["resume"])
                self.assertIsNone(kwargs["media_path"])
                return {
                    "model": youtube_asr.DEFAULT_MODEL,
                    "task_id": "task-channel-existing",
                    "delivery": "oss-signed-url",
                    "resolved_host": "oss.example",
                    "transcription": {
                        "transcripts": [
                            {
                                "sentences": [
                                    {"begin_time": 0, "end_time": 1000, "text": "恢复结果"}
                                ]
                            }
                        ]
                    },
                }

            with patch.object(youtube_channel_to_md, "download_caption") as captions, patch.object(
                youtube_channel_to_md, "get_video_metadata"
            ) as metadata, patch.object(
                youtube_channel_to_md, "download_audio"
            ) as download, patch.object(
                youtube_channel_to_md, "transcribe_media", side_effect=fake_transcribe
            ) as transcribe, patch.object(
                youtube_channel_to_md, "write_video_note"
            ) as write_note:
                exit_code = youtube_channel_to_md.main(
                    [CHANNEL_URL, "--run-dir", str(run_root), "--limit", "1"]
                )

            self.assertEqual(exit_code, 0)
            captions.assert_not_called()
            metadata.assert_not_called()
            download.assert_not_called()
            transcribe.assert_called_once()
            write_note.assert_called_once()
            saved = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "done")
            self.assertEqual(saved["items"][0]["status"], "captured")
            self.assertEqual(saved["items"][0]["task_id"], "task-channel-existing")


if __name__ == "__main__":
    unittest.main()
