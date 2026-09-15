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
import youtube_video_to_md  # noqa: E402


SOURCE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def transcript_result() -> dict[str, object]:
    return {
        "transcripts": [
            {"sentences": [{"begin_time": 0, "end_time": 1000, "text": "命令层恢复"}]}
        ]
    }


class YouTubeVideoResumeCliTests(unittest.TestCase):
    def test_resume_dir_preserves_manifest_and_passes_resume_flag(self) -> None:
        with TemporaryDirectory() as temp:
            output = Path(temp)
            metadata = youtube_asr.clean_video_metadata({}, SOURCE_URL)
            manifest = {
                "schema_version": 1,
                **youtube_asr.build_transcript_contract(
                    metadata,
                    scope="single",
                    status="cancelled",
                    transcript_source="unavailable",
                    asr_status="cancelled",
                    task_id="task-existing",
                ),
                "output_dir": str(output),
                "note_path": str(output / "note.md"),
                "custom_marker": "preserve-me",
                "stages": youtube_asr.initial_stage_states(),
            }
            youtube_asr.write_json(output / "metadata.json", metadata)
            youtube_asr.write_json(output / "manifest.json", manifest)
            youtube_asr.write_json(
                output / "video" / "submit.json",
                {"output": {"task_id": "task-existing"}},
            )
            youtube_asr.write_json(
                output / "video" / "task.json",
                {"output": {"task_status": "RUNNING"}},
            )

            def fake_transcribe(*args, **kwargs):
                self.assertTrue(kwargs["resume"])
                self.assertIsNone(kwargs["media_path"])
                return {
                    "status": "completed",
                    "model": youtube_asr.DEFAULT_MODEL,
                    "task_id": "task-existing",
                    "delivery": "oss-signed-url",
                    "resolved_host": "oss.example",
                    "oss_object_key": "youtube-asr/test.m4a",
                    "transcription": transcript_result(),
                }

            with patch.object(youtube_video_to_md, "transcribe_media", side_effect=fake_transcribe):
                exit_code = youtube_video_to_md.main(
                    [SOURCE_URL, "--resume-dir", str(output)]
                )

            self.assertEqual(exit_code, 0)
            saved_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["custom_marker"], "preserve-me")
            self.assertEqual(saved_manifest["task_id"], "task-existing")
            self.assertEqual(saved_manifest["status"], "captured")
            self.assertTrue((output / "note.md").is_file())


if __name__ == "__main__":
    unittest.main()
