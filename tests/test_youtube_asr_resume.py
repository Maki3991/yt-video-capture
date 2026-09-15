from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import youtube_asr  # noqa: E402


SOURCE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def transcript_result(text: str = "恢复测试文字") -> dict[str, object]:
    return {
        "transcripts": [
            {
                "sentences": [
                    {"begin_time": 0, "end_time": 1000, "text": text},
                ]
            }
        ]
    }


class YouTubeAsrResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_api_key = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = "test-only-key"

    def tearDown(self) -> None:
        if self.previous_api_key is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = self.previous_api_key

    def test_running_task_is_polled_without_upload_or_resubmit(self) -> None:
        with TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            youtube_asr.write_json(
                artifact_dir / "request-info.json",
                {
                    "schema_version": 1,
                    "status": "cancelled",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "source_url": SOURCE_URL,
                    "attempt": 1,
                    "task_id": "task-existing",
                },
            )
            youtube_asr.write_json(
                artifact_dir / "submit.json",
                {"output": {"task_id": "task-existing"}},
            )

            def fake_api(method: str, uri: str, api_key: str, body=None, **kwargs):
                self.assertEqual(method, "GET")
                self.assertTrue(uri.endswith("/tasks/task-existing"))
                if fake_api.calls == 0:
                    fake_api.calls += 1
                    return {"output": {"task_status": "RUNNING"}}
                return {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://result.example/transcription.json?token=secret",
                            }
                        ],
                    }
                }

            fake_api.calls = 0
            with patch.object(youtube_asr, "_api_json", side_effect=fake_api) as api, patch.object(
                youtube_asr, "_download_json", return_value=transcript_result()
            ) as download, patch.object(
                youtube_asr, "upload_local_media_to_oss"
            ) as upload, patch.object(youtube_asr.time, "sleep"):
                result = youtube_asr.transcribe_media(
                    SOURCE_URL,
                    artifact_dir,
                    media_path=None,
                    resume=True,
                    poll_interval=1,
                    timeout=30,
                )

            self.assertEqual(result["task_id"], "task-existing")
            self.assertTrue(result["resumed"])
            self.assertEqual(api.call_count, 2)
            self.assertEqual(download.call_count, 1)
            upload.assert_not_called()
            self.assertTrue((artifact_dir / "transcript.md").is_file())
            request_info = json.loads((artifact_dir / "request-info.json").read_text(encoding="utf-8"))
            self.assertEqual(request_info["task_id"], "task-existing")
            self.assertEqual(request_info["attempt"], 1)
            self.assertNotIn("token=secret", (artifact_dir / "task.json").read_text(encoding="utf-8"))

    def test_failed_task_is_archived_before_new_attempt(self) -> None:
        with TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            media_path = artifact_dir / "source.m4a"
            media_path.write_bytes(b"test media")
            youtube_asr.write_json(
                artifact_dir / "request-info.json",
                {
                    "schema_version": 1,
                    "status": "failed",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "source_url": SOURCE_URL,
                    "attempt": 1,
                    "task_id": "task-failed",
                },
            )
            youtube_asr.write_json(
                artifact_dir / "submit.json",
                {"output": {"task_id": "task-failed"}},
            )
            youtube_asr.write_json(
                artifact_dir / "task.json",
                {"output": {"task_status": "FAILED"}},
            )

            def fake_api(method: str, uri: str, api_key: str, body=None, **kwargs):
                if method == "POST":
                    return {"output": {"task_id": "task-new"}}
                self.assertEqual(method, "GET")
                return {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://result.example/new.json?token=new-secret",
                            }
                        ],
                    }
                }

            fake_oss = {
                "signed_url": "https://oss.example/media?signature=secret",
                "bucket": "test-bucket",
                "object_key": "youtube-asr/test.m4a",
                "local_path": str(media_path),
                "size_bytes": media_path.stat().st_size,
                "content_type": "audio/mp4",
                "signed_url_expires": 3600,
                "uploaded_at": "2026-09-15T00:00:00+00:00",
            }
            with patch.object(youtube_asr, "_api_json", side_effect=fake_api) as api, patch.object(
                youtube_asr, "_download_json", return_value=transcript_result("新 attempt")
            ), patch.object(
                youtube_asr, "upload_local_media_to_oss", return_value=fake_oss
            ) as upload:
                result = youtube_asr.transcribe_media(
                    SOURCE_URL,
                    artifact_dir,
                    media_path=media_path,
                    resume=True,
                    poll_interval=1,
                    timeout=30,
                )

            self.assertEqual(result["task_id"], "task-new")
            self.assertEqual(result["resumed"], False)
            self.assertEqual(upload.call_count, 1)
            self.assertEqual(
                [call.args[0] for call in api.call_args_list],
                ["POST", "GET"],
            )
            archived_submit = artifact_dir / "attempts" / "001" / "submit.json"
            self.assertTrue(archived_submit.is_file())
            self.assertIn("task-failed", archived_submit.read_text(encoding="utf-8"))
            request_info = json.loads((artifact_dir / "request-info.json").read_text(encoding="utf-8"))
            self.assertEqual(request_info["attempt"], 2)
            self.assertEqual(request_info["task_id"], "task-new")

    def test_result_download_can_resume_without_new_submission(self) -> None:
        with TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            youtube_asr.write_json(
                artifact_dir / "request-info.json",
                {
                    "schema_version": 1,
                    "status": "submitted",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "source_url": SOURCE_URL,
                    "attempt": 1,
                    "task_id": "task-result-retry",
                },
            )
            youtube_asr.write_json(
                artifact_dir / "submit.json",
                {"output": {"task_id": "task-result-retry"}},
            )
            youtube_asr.write_json(
                artifact_dir / "task.json",
                {"output": {"task_status": "SUCCEEDED"}},
            )

            successful_task = {
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "https://result.example/retry.json?token=retry-secret",
                        }
                    ],
                }
            }
            with patch.object(
                youtube_asr, "_api_json", return_value=successful_task
            ) as api, patch.object(
                youtube_asr, "_download_json", side_effect=youtube_asr.YouTubeError("temporary download failure")
            ) as download, patch.object(
                youtube_asr, "upload_local_media_to_oss"
            ) as upload:
                with self.assertRaises(youtube_asr.YouTubeError):
                    youtube_asr.transcribe_media(
                        SOURCE_URL,
                        artifact_dir,
                        media_path=None,
                        resume=True,
                        poll_interval=1,
                        timeout=30,
                    )

            api.assert_called_once()
            download.assert_called_once()
            upload.assert_not_called()
            self.assertTrue((artifact_dir / "error.json").is_file())

            with patch.object(
                youtube_asr, "_api_json", return_value=successful_task
            ) as retry_api, patch.object(
                youtube_asr, "_download_json", return_value=transcript_result("重试后结果")
            ) as retry_download, patch.object(
                youtube_asr, "upload_local_media_to_oss"
            ) as retry_upload:
                result = youtube_asr.transcribe_media(
                    SOURCE_URL,
                    artifact_dir,
                    media_path=None,
                    resume=True,
                    poll_interval=1,
                    timeout=30,
                )

            self.assertEqual(result["task_id"], "task-result-retry")
            self.assertTrue(result["resumed"])
            retry_api.assert_called_once()
            retry_download.assert_called_once()
            retry_upload.assert_not_called()
            self.assertTrue((artifact_dir / "transcript.md").is_file())
            self.assertFalse((artifact_dir / "attempts").exists())

    def test_completed_local_checkpoint_needs_no_new_api_call(self) -> None:
        with TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            youtube_asr.write_json(
                artifact_dir / "request-info.json",
                {
                    "schema_version": 1,
                    "status": "completed",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "source_url": SOURCE_URL,
                    "attempt": 1,
                    "task_id": "task-complete",
                    "delivery": "oss-signed-url",
                    "resolved_host": "oss.example",
                    "oss_object_key": "youtube-asr/test.m4a",
                },
            )
            youtube_asr.write_json(
                artifact_dir / "submit.json",
                {"output": {"task_id": "task-complete"}},
            )
            youtube_asr.write_json(artifact_dir / "transcription.json", transcript_result())
            (artifact_dir / "transcript.md").write_text("# 已有结果\n", encoding="utf-8")

            with patch.object(youtube_asr, "_api_json") as api, patch.object(
                youtube_asr, "upload_local_media_to_oss"
            ) as upload:
                result = youtube_asr.transcribe_media(
                    SOURCE_URL,
                    artifact_dir,
                    media_path=None,
                    resume=True,
                )

            self.assertEqual(result["task_id"], "task-complete")
            api.assert_not_called()
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
