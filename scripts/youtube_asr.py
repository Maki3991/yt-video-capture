#!/usr/bin/env python3
"""Shared YouTube metadata, yt-dlp and Bailian transcription helpers.

The module deliberately keeps the first YouTube implementation independent from
the XHS Skill. Both Skills use the same evidence contract, while their page
collection steps remain separate. The ASR media path is deliberately local-first:

    YouTube page -> local audio file -> private OSS signed URL -> Bailian async ASR

The module does not expose a YouTube CDN direct-link delivery path. Temporary
OSS signed URLs are only held in memory long enough to submit/download a task;
persisted JSON diagnostics redact their query values.
"""

from __future__ import annotations

import importlib.util
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


DEFAULT_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DEFAULT_API_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 7200
DEFAULT_COOKIE_DIR = Path(r"D:\Softwares\Programming Projects\_yt-cookies")
DEFAULT_COOKIE_FILENAME = "youtube-cookies.txt"
COOKIE_FILE_SUFFIXES = {".txt", ".cookie", ".cookies"}
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "UNKNOWN"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
VIDEO_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")
SENSITIVE_URL_KEYS = {
    "resolved_url",
    "direct_url",
    "file_url",
    "file_urls",
    "signed_url",
    "transcription_url",
}
LOCAL_ENV_KEYS = {
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
}
OUTPUT_CONTRACT_VERSION = 1
CAPTION_FORMAT_PREFERENCE = ("vtt", "srt", "srv3", "srv1", "ttml", "json3")
CAPTION_TYPE_ORDER = {"manual": 0, "automatic": 1, "translated": 2}
PIPELINE_STAGES = (
    "metadata",
    "browser_transcript",
    "media_download",
    "oss_upload",
    "asr_submit",
    "asr_poll",
    "transcript_download",
    "markdown_render",
)
ASR_CHECKPOINT_FILES = (
    "request-info.json",
    "oss.json",
    "submit.json",
    "task.json",
    "transcription.json",
    "error.json",
    "transcript.md",
)
STAGE_STATUSES = {
    "pending",
    "running",
    "succeeded",
    "skipped",
    "failed",
    "cancelled",
}
StageCallback = Callable[..., None]


class YouTubeError(RuntimeError):
    """A user-facing, non-secret error from the YouTube pipeline."""


def load_local_env() -> None:
    """Load whitelisted values from the Skill-local .env without printing them."""

    skill_root = Path(__file__).resolve().parents[1]
    paths: list[Path] = []
    for candidate in (
        skill_root / ".env",
        skill_root / ".env.local",
        Path.cwd() / ".env",
        Path.cwd() / ".env.local",
    ):
        if candidate not in paths:
            paths.append(candidate)

    values: dict[str, str] = {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in LOCAL_ENV_KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value

    for key, value in values.items():
        os.environ.setdefault(key, value)


load_local_env()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_stage_states() -> dict[str, dict[str, Any]]:
    """Return the durable per-video stage state used by manifests."""

    return {
        stage: {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "attempt": 0,
            "retryable": False,
            "error": None,
            "artifact_paths": [],
            "reason": None,
        }
        for stage in PIPELINE_STAGES
    }


def update_stage_state(
    stages: dict[str, Any],
    stage: str,
    status: str,
    *,
    artifact_paths: Iterable[str] | None = None,
    error: str | None = None,
    retryable: bool | None = None,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Update one stage without discarding evidence from earlier attempts."""

    if stage not in PIPELINE_STAGES:
        raise YouTubeError(f"未知的处理阶段：{stage}")
    if status not in STAGE_STATUSES:
        raise YouTubeError(f"未知的阶段状态：{status}")

    previous = stages.get(stage)
    previous = previous if isinstance(previous, dict) else {}
    previous_status = str(previous.get("status") or "pending")
    try:
        previous_attempt = int(previous.get("attempt") or 0)
    except (TypeError, ValueError):
        previous_attempt = 0
    timestamp = now or utc_now()
    previous_paths = previous.get("artifact_paths")
    previous_paths = previous_paths if isinstance(previous_paths, list) else []
    record: dict[str, Any] = {
        "status": status,
        "started_at": previous.get("started_at"),
        "completed_at": previous.get("completed_at"),
        "attempt": max(0, previous_attempt),
        "retryable": bool(previous.get("retryable", False)),
        "error": previous.get("error"),
        "artifact_paths": list(previous_paths),
    }

    if status == "pending":
        record.update(
            {
                "started_at": None,
                "completed_at": None,
                "retryable": False if retryable is None else bool(retryable),
                "error": None,
                "reason": reason,
            }
        )
    elif status == "running":
        attempt = previous_attempt if previous_status == "running" else previous_attempt + 1
        record.update(
            {
                "started_at": timestamp,
                "completed_at": None,
                "attempt": max(1, attempt),
                "retryable": True if retryable is None else bool(retryable),
                "error": None,
                "reason": reason,
            }
        )
    else:
        record.update(
            {
                "started_at": previous.get("started_at") or timestamp,
                "completed_at": timestamp,
                "attempt": max(1, previous_attempt),
                "retryable": (
                    False if status in {"succeeded", "skipped"} else True
                    if retryable is None
                    else bool(retryable)
                ),
                "error": str(error)[:3000] if error else None,
                "reason": reason,
            }
        )

    for raw_path in artifact_paths or []:
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if normalized and normalized not in record["artifact_paths"]:
            record["artifact_paths"].append(normalized)
    stages[stage] = record
    return record


def artifact_path_for_output(
    path: Path | str,
    *,
    artifact_root: Path,
    output_root: Path,
) -> str:
    """Represent an artifact relative to the run root without leaking host paths."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = artifact_root / candidate
    candidate = candidate.resolve()
    output_root = output_root.expanduser().resolve()
    try:
        return candidate.relative_to(output_root).as_posix()
    except ValueError:
        return f"external:{candidate.name}"


def build_transcript_contract(
    metadata: dict[str, Any],
    *,
    scope: str,
    status: str,
    transcript_source: str | None,
    caption_language: str | None = None,
    caption_type: str | None = None,
    asr_status: str | None = None,
    asr_model: str | None = None,
    task_id: str | None = None,
    media_delivery: str | None = None,
    review_status: str = "unreviewed",
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Build the shared note/item metadata contract for caption and ASR paths."""

    if asr_status is None:
        if transcript_source == "platform_caption":
            asr_status = "skipped"
        elif transcript_source == "asr" and status == "captured":
            asr_status = "completed"
        elif status == "failed":
            asr_status = "failed"
        else:
            asr_status = "pending"
    return {
        "contract_version": OUTPUT_CONTRACT_VERSION,
        "platform": "youtube",
        "source_url": metadata.get("source_url") or None,
        "source_id": metadata.get("source_id") or None,
        "author": metadata.get("author") or None,
        "channel_url": metadata.get("channel_url") or None,
        "title": str(
            metadata.get("title") or metadata.get("source_id") or "未命名视频"
        ).strip(),
        "published_at": metadata.get("published_at") or None,
        "captured_at": captured_at or utc_now(),
        "scope": scope,
        "transcript_source": transcript_source or None,
        "caption_language": caption_language or None,
        "caption_type": caption_type or None,
        "asr_status": asr_status,
        "asr_model": asr_model or None,
        "task_id": task_id or None,
        "media_delivery": media_delivery or None,
        "status": status,
        "review_status": review_status or "unreviewed",
        "duration_seconds": metadata.get("duration") or None,
    }


def write_json(path: Path, value: Any, *, sanitize: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sanitize:
        value = sanitize_for_persist(value)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubeError(f"无法读取 JSON：{path}\n{exc}") from exc
    if not isinstance(value, dict):
        raise YouTubeError(f"JSON 顶层不是对象：{path}")
    return value


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return read_json(path)


def _task_id_from_payload(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return ""
    direct = str(value.get("task_id") or "").strip()
    if direct:
        return direct
    output = value.get("output")
    output = output if isinstance(output, dict) else {}
    return str(output.get("task_id") or "").strip()


def _task_status(task: dict[str, Any] | None) -> str:
    if not isinstance(task, dict):
        return ""
    output = task.get("output")
    output = output if isinstance(output, dict) else {}
    return str(output.get("task_status") or "").strip().upper()


def _asr_attempt_number(artifact_dir: Path) -> int:
    """Find the latest durable ASR attempt number without reading secrets."""

    attempt = 0
    request_info = _read_optional_json(artifact_dir / "request-info.json")
    if request_info:
        try:
            attempt = max(attempt, int(request_info.get("attempt") or 0))
        except (TypeError, ValueError):
            pass
    attempts_dir = artifact_dir / "attempts"
    if attempts_dir.is_dir():
        for child in attempts_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                attempt = max(attempt, int(child.name))
            except ValueError:
                continue
    if attempt == 0 and any((artifact_dir / name).is_file() for name in ASR_CHECKPOINT_FILES):
        attempt = 1
    return attempt


def _load_asr_checkpoint(artifact_dir: Path) -> dict[str, Any]:
    artifact_dir = artifact_dir.expanduser().resolve()
    request_info = _read_optional_json(artifact_dir / "request-info.json")
    submit = _read_optional_json(artifact_dir / "submit.json")
    task = _read_optional_json(artifact_dir / "task.json")
    transcription = _read_optional_json(artifact_dir / "transcription.json")
    task_id = (
        _task_id_from_payload(submit)
        or _task_id_from_payload(request_info)
        or _task_id_from_payload(task)
    )
    return {
        "request_info": request_info or {},
        "submit": submit or {},
        "task": task,
        "transcription": transcription,
        "task_id": task_id,
        "task_status": _task_status(task),
        "attempt": _asr_attempt_number(artifact_dir),
    }


def asr_checkpoint_state(artifact_dir: Path) -> dict[str, Any]:
    """Return the non-sensitive part of an ASR checkpoint for callers."""

    checkpoint = _load_asr_checkpoint(artifact_dir)
    transcription = checkpoint.get("transcription")
    return {
        "task_id": checkpoint.get("task_id") or None,
        "task_status": checkpoint.get("task_status") or None,
        "attempt": checkpoint.get("attempt") or 0,
        "has_transcription": bool(
            isinstance(transcription, dict) and transcript_has_content(transcription)
        ),
    }


def _archive_current_attempt(
    artifact_dir: Path,
    attempt: int,
    *,
    task_id: str | None,
    task_status: str | None,
) -> Path | None:
    """Preserve the active checkpoint before a new ASR attempt replaces it."""

    if attempt <= 0:
        return None
    existing = [artifact_dir / name for name in ASR_CHECKPOINT_FILES if (artifact_dir / name).is_file()]
    if not existing:
        return None
    target = artifact_dir / "attempts" / f"{attempt:03d}"
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in existing:
        destination = target / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
        copied.append(destination.name)
    write_json(
        target / "attempt.json",
        {
            "schema_version": 1,
            "attempt": attempt,
            "task_id": task_id or None,
            "task_status": task_status or None,
            "archived_at": utc_now(),
            "artifact_files": copied,
        },
        sanitize=True,
    )
    return target


def _clear_active_attempt(artifact_dir: Path) -> None:
    """Remove replaceable root checkpoints while keeping local media intact."""

    for name in ASR_CHECKPOINT_FILES:
        path = artifact_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def redact_url(value: str) -> str:
    """Keep a URL's shape while removing query values such as signatures."""

    try:
        parsed = urlparse(value)
        if not parsed.query:
            return value
        redacted_query = urlencode(
            [
                (key, "<redacted>")
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunparse(parsed._replace(query=redacted_query))
    except ValueError:
        return "<redacted-url>"


def sanitize_for_persist(value: Any, key: str = "") -> Any:
    """Redact only signed media/result URLs in diagnostic JSON."""

    if isinstance(value, dict):
        return {
            str(child_key): sanitize_for_persist(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_persist(item, key) for item in value]
    if key in SENSITIVE_URL_KEYS and isinstance(value, str):
        return redact_url(value)
    return value


def safe_error(value: object, source_url: str = "", sensitive_url: str = "") -> str:
    message = str(value).strip()
    if source_url:
        message = message.replace(source_url, "<source-url>")
    if sensitive_url:
        message = message.replace(sensitive_url, "<signed-media-url>")
    return message[:3000]


def _find_yt_dlp(explicit: str | None) -> list[str]:
    candidates = [
        explicit,
        os.environ.get("YOUTUBE_YTDLP"),
        os.environ.get("YT_DLP"),
        # The existing XHS Skill uses this variable. Reusing it is harmless
        # and makes the two local media Skills easier to operate together.
        os.environ.get("XHS_YTDLP"),
    ]
    for candidate in candidates:
        if candidate and candidate.strip():
            return [candidate.strip()]

    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]

    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]

    raise YouTubeError(
        "找不到 yt-dlp。请将 yt-dlp 加入 PATH，或设置 YOUTUBE_YTDLP，"
        "也可以使用 --yt-dlp 指定可执行文件路径。"
    )


def _yt_dlp_js_runtime_args() -> list[str]:
    """Enable yt-dlp's YouTube challenge solver when Node.js is available."""

    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []


def _default_cookie_file() -> Path | None:
    """Find the user's optional fixed Netscape-format Cookie file."""

    configured_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if configured_file:
        cookie_path = Path(configured_file).expanduser().resolve()
        if not cookie_path.is_file():
            raise YouTubeError(f"Cookie 文件不存在：{cookie_path}")
        return cookie_path

    configured_dir = os.environ.get("YOUTUBE_COOKIES_DIR", "").strip()
    cookie_dir = (
        Path(configured_dir).expanduser().resolve()
        if configured_dir
        else DEFAULT_COOKIE_DIR
    )
    if not cookie_dir.is_dir():
        return None

    preferred = cookie_dir / DEFAULT_COOKIE_FILENAME
    if preferred.is_file():
        return preferred.resolve()

    candidates = [
        path
        for path in cookie_dir.iterdir()
        if path.is_file() and path.suffix.lower() in COOKIE_FILE_SUFFIXES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()


def _run_yt_dlp(
    args: list[str],
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if cookies_from_browser and cookies_file:
        raise YouTubeError("--cookies-from-browser 与 --cookies 只能二选一")
    if not cookies_from_browser and cookies_file is None:
        cookies_file = _default_cookie_file()
    command = [
        *_find_yt_dlp(explicit_yt_dlp),
        "--ignore-config",
        *_yt_dlp_js_runtime_args(),
    ]
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    if cookies_file:
        cookie_path = Path(cookies_file).expanduser().resolve()
        if not cookie_path.is_file():
            raise YouTubeError(f"Cookie 文件不存在：{cookie_path}")
        command.extend(["--cookies", str(cookie_path)])
    command.extend(args)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            check=False,
        )
    except OSError as exc:
        raise YouTubeError(f"无法启动 yt-dlp：{exc}") from exc


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    raw = stdout.strip()
    if not raw:
        raise YouTubeError("yt-dlp 没有返回 JSON。")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Some extractor versions may emit one JSON object per line even with
        # -J. Try the last JSON-looking line without accepting log text.
        value = None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise YouTubeError("无法解析 yt-dlp 返回的 JSON。")
    if not isinstance(value, dict):
        raise YouTubeError("yt-dlp 返回的 JSON 顶层不是对象。")
    return value


def _host_matches(host: str) -> bool:
    host = (host or "").lower().split(":", 1)[0]
    return host in YOUTUBE_HOSTS or host.endswith(".youtube.com")


def video_id_from_url(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    query_id = next(
        (item for item in parse_qsl(parsed.query) if item[0] == "v" and item[1]),
        ("", ""),
    )[1]
    if query_id:
        return query_id
    path = parsed.path.strip("/")
    for prefix in ("shorts/", "live/", "embed/", "v/"):
        if path.startswith(prefix):
            return path[len(prefix) :].split("/", 1)[0]
    return ""


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and _host_matches(parsed.hostname or "")


def validate_video_url(value: str) -> str:
    source_url = value.strip()
    if not is_youtube_url(source_url):
        raise YouTubeError("输入必须是 YouTube 的 HTTP(S) 链接。")
    video_id = video_id_from_url(source_url)
    if not video_id:
        raise YouTubeError(
            "输入看起来不是单个 YouTube 视频链接；请使用 /watch?v=、/shorts/、"
            "/live/ 或 youtu.be/ 链接。"
        )
    return source_url


def validate_channel_url(value: str) -> str:
    source_url = value.strip()
    if not is_youtube_url(source_url):
        raise YouTubeError("输入必须是 YouTube 频道的 HTTP(S) 链接。")
    if video_id_from_url(source_url):
        raise YouTubeError("频道批量入口收到的是视频链接；请改用频道主页或频道 /videos 链接。")
    parsed = urlparse(source_url)
    path = parsed.path.rstrip("/")
    if not (
        path.startswith("/@")
        or path.startswith("/channel/")
        or path.startswith("/c/")
        or path.startswith("/user/")
    ):
        raise YouTubeError(
            "无法识别频道链接。请提供 https://www.youtube.com/@频道名、"
            "/channel/<ID>、/c/<名称> 或 /user/<名称>。"
        )
    return source_url


def channel_tab_url(channel_url: str, tab: str = "videos") -> str:
    validate_channel_url(channel_url)
    if tab not in {"videos", "shorts", "streams"}:
        raise YouTubeError("频道标签只能是 videos、shorts 或 streams。")
    parsed = urlparse(channel_url.strip())
    path = parsed.path.rstrip("/")
    if not path.endswith(f"/{tab}"):
        path = f"{path}/{tab}"
    return urlunparse(parsed._replace(path=path, fragment="", query=""))


def _published_at(raw: dict[str, Any]) -> str | None:
    timestamp = raw.get("timestamp") or raw.get("release_timestamp")
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
                timespec="seconds"
            )
        except (OverflowError, OSError, ValueError):
            pass
    upload_date = str(raw.get("upload_date") or "").strip()
    if re.fullmatch(r"\d{8}", upload_date):
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return None


def _caption_format_rank(extension: str) -> int:
    normalized = str(extension or "").strip().lower().lstrip(".")
    try:
        return CAPTION_FORMAT_PREFERENCE.index(normalized)
    except ValueError:
        return len(CAPTION_FORMAT_PREFERENCE)


def _caption_track_type(
    base_type: str,
    language: str,
    caption_url: str,
    raw_track: dict[str, Any],
) -> str:
    """Identify translated tracks without treating them as original captions."""

    query_keys = {key.lower() for key, _ in parse_qsl(urlparse(caption_url).query)}
    name = str(raw_track.get("name") or "").lower()
    if "tlang" in query_keys or "translated" in name:
        return "translated"
    if str(language).lower().endswith("-orig"):
        return base_type
    return base_type


def _caption_tracks_from_raw(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize yt-dlp caption metadata while keeping signed URLs in memory only."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for section, base_type in (
        ("subtitles", "manual"),
        ("automatic_captions", "automatic"),
    ):
        language_map = raw.get(section)
        if not isinstance(language_map, dict):
            continue
        for raw_language, raw_formats in language_map.items():
            language = str(raw_language or "").strip()
            if not language or not isinstance(raw_formats, list):
                continue
            for raw_track in raw_formats:
                if not isinstance(raw_track, dict):
                    continue
                caption_url = str(raw_track.get("url") or "").strip()
                if not caption_url:
                    continue
                extension = str(raw_track.get("ext") or "").strip().lower().lstrip(".")
                if not extension:
                    extension = "vtt"
                caption_type = _caption_track_type(
                    base_type,
                    language,
                    caption_url,
                    raw_track,
                )
                candidate = {
                    "language": language,
                    "caption_type": caption_type,
                    "ext": extension,
                    "url": caption_url,
                }
                group_key = (language, caption_type)
                previous = grouped.get(group_key)
                if previous is None or _caption_format_rank(extension) < _caption_format_rank(
                    str(previous.get("ext") or "")
                ):
                    grouped[group_key] = candidate

    return sorted(
        grouped.values(),
        key=lambda item: (
            CAPTION_TYPE_ORDER.get(str(item.get("caption_type") or ""), 99),
            str(item.get("language") or "").lower(),
            _caption_format_rank(str(item.get("ext") or "")),
        ),
    )


def _public_caption_track(track: dict[str, Any]) -> dict[str, str]:
    """Return a safe caption descriptor that deliberately omits its URL."""

    return {
        "language": str(track.get("language") or ""),
        "type": str(track.get("caption_type") or ""),
        "format": str(track.get("ext") or ""),
    }


def _language_matches(actual: str, requested: str) -> tuple[int, int]:
    actual_normalized = str(actual or "").strip().lower().replace("_", "-")
    requested_normalized = str(requested or "").strip().lower().replace("_", "-")
    if not actual_normalized or not requested_normalized:
        return (0, 0)
    if actual_normalized == requested_normalized:
        return (2, 0)
    actual_base = actual_normalized.split("-", 1)[0]
    requested_base = requested_normalized.split("-", 1)[0]
    if actual_base == requested_base:
        return (1, 0)
    return (0, 0)


def _select_caption_track(
    tracks: list[dict[str, Any]],
    language_preferences: Iterable[str] | None = None,
    *,
    allow_translated: bool = False,
) -> dict[str, Any] | None:
    candidates = [
        track
        for track in tracks
        if allow_translated or track.get("caption_type") != "translated"
    ]
    preferences = [
        str(item).strip()
        for item in (language_preferences or [])
        if str(item).strip()
    ]
    if not candidates or (preferences and not any(
        _language_matches(str(track.get("language") or ""), preference)[0]
        for preference in preferences
        for track in candidates
    )):
        return None
    if not preferences:
        return candidates[0]

    for preference in preferences:
        matches = [
            track
            for track in candidates
            if _language_matches(str(track.get("language") or ""), preference)[0]
        ]
        if matches:
            return sorted(
                matches,
                key=lambda item: (
                    -_language_matches(
                        str(item.get("language") or ""), preference
                    )[0],
                    CAPTION_TYPE_ORDER.get(str(item.get("caption_type") or ""), 99),
                ),
            )[0]
    return None


def _video_info_with_yt_dlp(
    source_url: str,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> dict[str, Any]:
    source_url = validate_video_url(source_url)
    completed = _run_yt_dlp(
        [
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--quiet",
            "--retries",
            "3",
            "--socket-timeout",
            "30",
            "--dump-single-json",
            source_url,
        ],
        explicit_yt_dlp,
        cookies_from_browser,
        cookies_file,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or "").strip()
        raise YouTubeError(
            "yt-dlp 无法读取视频元数据。\n"
            + safe_error(diagnostic[-2000:] or f"return code {completed.returncode}", source_url)
        )
    return _parse_json_stdout(completed.stdout)


def clean_video_metadata(raw: dict[str, Any], source_url: str = "") -> dict[str, Any]:
    """Keep useful metadata while excluding yt-dlp's signed format URLs."""

    fields = (
        "id",
        "title",
        "description",
        "channel",
        "channel_id",
        "channel_url",
        "uploader",
        "uploader_id",
        "uploader_url",
        "upload_date",
        "timestamp",
        "release_timestamp",
        "duration",
        "duration_string",
        "view_count",
        "like_count",
        "categories",
        "tags",
        "thumbnail",
        "webpage_url",
        "original_url",
        "availability",
        "live_status",
        "was_live",
        "extractor",
        "extractor_key",
    )
    result = {key: raw[key] for key in fields if key in raw and raw[key] is not None}
    source_id = str(raw.get("id") or video_id_from_url(source_url) or "").strip()
    canonical_url = str(raw.get("webpage_url") or source_url).strip()
    result.update(
        {
            "source_url": source_url or canonical_url,
            "canonical_url": canonical_url,
            "source_id": source_id,
            "title": str(raw.get("title") or source_id or "未命名视频").strip(),
            "author": str(raw.get("channel") or raw.get("uploader") or "").strip(),
            "published_at": _published_at(raw),
            "caption_tracks": [
                _public_caption_track(track) for track in _caption_tracks_from_raw(raw)
            ],
        }
    )
    return sanitize_for_persist(result)


def get_video_metadata(
    source_url: str,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> dict[str, Any]:
    source_url = validate_video_url(source_url)
    raw = _video_info_with_yt_dlp(
        source_url,
        explicit_yt_dlp,
        cookies_from_browser,
        cookies_file,
    )
    return clean_video_metadata(raw, source_url)


def get_caption_tracks(
    source_url: str,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read available caption tracks; signed track URLs stay in process memory."""

    raw = _video_info_with_yt_dlp(
        source_url,
        explicit_yt_dlp,
        cookies_from_browser,
        cookies_file,
    )
    return _caption_tracks_from_raw(raw)


def enumerate_channel(
    channel_url: str,
    limit: int,
    *,
    tab: str = "videos",
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> dict[str, Any]:
    """Collect the first N publicly listed entries from a channel tab.

    "First" is the order returned by yt-dlp for the public channel tab in this
    run. It is not a claim about private, members-only, deleted or hidden
    videos.
    """

    channel_url = validate_channel_url(channel_url)
    if limit <= 0:
        raise YouTubeError("--limit 必须是正整数。")
    tab_url = channel_tab_url(channel_url, tab)
    completed = _run_yt_dlp(
        [
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--ignore-errors",
            "--skip-download",
            "--no-warnings",
            "--quiet",
            "--retries",
            "3",
            "--socket-timeout",
            "30",
            "--dump-single-json",
            tab_url,
        ],
        explicit_yt_dlp,
        cookies_from_browser,
        cookies_file,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or "").strip()
        raise YouTubeError(
            "yt-dlp 无法读取频道列表。\n"
            + safe_error(diagnostic[-2000:] or f"return code {completed.returncode}", channel_url)
        )
    raw = _parse_json_stdout(completed.stdout)
    raw_entries = raw.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    videos: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        entry_url = str(entry.get("webpage_url") or "").strip()
        if not entry_url and entry_id:
            entry_url = f"https://www.youtube.com/watch?v={entry_id}"
        if not entry_id:
            entry_id = video_id_from_url(entry_url)
        if not entry_id or entry_id in seen_ids:
            continue
        if not entry_url:
            entry_url = f"https://www.youtube.com/watch?v={entry_id}"
        seen_ids.add(entry_id)
        item = clean_video_metadata(entry, entry_url)
        item["index"] = len(videos) + 1
        videos.append(item)
        if len(videos) >= limit:
            break

    channel_meta = {
        "title": raw.get("title"),
        "channel": raw.get("channel") or raw.get("uploader"),
        "channel_id": raw.get("channel_id") or raw.get("uploader_id"),
        "channel_url": raw.get("channel_url") or raw.get("uploader_url"),
        "playlist_count": raw.get("playlist_count"),
    }
    return {
        "schema_version": 1,
        "source_url": channel_url,
        "tab_url": tab_url,
        "tab": tab,
        "requested_count": limit,
        "collected_count": len(videos),
        "sufficient": len(videos) >= limit,
        "collection_order": "yt-dlp public channel tab order for this run",
        "channel": sanitize_for_persist(channel_meta),
        "videos": videos,
        "warning": "" if len(videos) >= limit else f"只发现 {len(videos)} 条可处理视频，未达到请求的 {limit} 条。",
    }


def download_audio(
    source_url: str,
    output_dir: Path,
    *,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
    format_selector: str = "bestaudio[ext=m4a]/bestaudio/best",
) -> Path:
    """Download one audio track locally for the OSS delivery route."""

    source_url = validate_video_url(source_url)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "source.%(ext)s")
    completed = _run_yt_dlp(
        [
            "--no-playlist",
            "--newline",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--socket-timeout",
            "30",
            "-f",
            format_selector,
            "-o",
            template,
            source_url,
        ],
        explicit_yt_dlp,
        cookies_from_browser,
        cookies_file,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        raise YouTubeError(
            "yt-dlp 无法下载本地音频。\n"
            + safe_error(diagnostic[-2500:] or f"return code {completed.returncode}", source_url)
        )

    candidates = [
        path
        for path in output_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl"}
    ]
    if not candidates:
        raise YouTubeError(f"yt-dlp 下载完成但没有找到音频文件：{output_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _oss_settings() -> tuple[str, str, str, str]:
    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
    endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
    bucket_name = os.environ.get("OSS_BUCKET", "").strip()
    missing = [
        name
        for name, value in (
            ("OSS_ACCESS_KEY_ID", access_key_id),
            ("OSS_ACCESS_KEY_SECRET", access_key_secret),
            ("OSS_ENDPOINT", endpoint),
            ("OSS_BUCKET", bucket_name),
        )
        if not value
    ]
    if missing:
        raise YouTubeError(
            "OSS 配置不完整，缺少："
            + ", ".join(missing)
            + "。请在 Skill 目录的 .env 中填写，或在当前进程设置同名环境变量。"
        )
    return access_key_id, access_key_secret, endpoint, bucket_name


def upload_local_media_to_oss(
    media_path: Path,
    object_key: str,
    *,
    signed_url_expires: int = 3600,
) -> dict[str, Any]:
    """Upload a local media file to private OSS and return a short-lived GET URL."""

    media_path = media_path.expanduser().resolve()
    if not media_path.is_file():
        raise YouTubeError(f"找不到待上传的本地媒体文件：{media_path}")
    if signed_url_expires <= 0:
        raise YouTubeError("OSS 签名 URL 有效期必须是正整数秒数。")

    try:
        import oss2
    except ImportError as exc:
        raise YouTubeError(
            "当前 Python 环境没有安装 oss2。请先执行：python -m pip install oss2"
        ) from exc

    access_key_id, access_key_secret, endpoint, bucket_name = _oss_settings()
    object_key = object_key.strip().lstrip("/")
    if not object_key:
        raise YouTubeError("OSS object key 不能为空。")

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    result = bucket.put_object_from_file(
        object_key,
        str(media_path),
        headers={"Content-Type": content_type},
    )
    response_status = getattr(result, "status", None)
    if response_status not in (None, 200):
        raise YouTubeError(f"OSS 上传失败，HTTP 状态：{response_status}")

    metadata = bucket.head_object(object_key)
    signed_url = bucket.sign_url("GET", object_key, signed_url_expires)
    return {
        "bucket": bucket_name,
        "object_key": object_key,
        "local_path": str(media_path),
        "size_bytes": int(getattr(metadata, "content_length", media_path.stat().st_size)),
        "content_type": content_type,
        "signed_url": signed_url,
        "signed_url_expires": signed_url_expires,
        "uploaded_at": utc_now(),
    }


def _api_json(
    method: str,
    uri: str,
    api_key: str,
    body: dict[str, Any] | None = None,
    *,
    asynchronous: bool = False,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if asynchronous:
        headers["X-DashScope-Async"] = "enable"

    last_error: Exception | None = None
    for attempt in range(3):
        request = Request(uri, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise YouTubeError("百炼 API 返回的 JSON 顶层不是对象。")
            return value
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                detail = str(exc)
            last_error = YouTubeError(f"百炼 API HTTP {exc.code}: {detail[:1500]}")
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (URLError, UnicodeDecodeError, json.JSONDecodeError, YouTubeError) as exc:
            if isinstance(exc, YouTubeError) and "顶层不是对象" in str(exc):
                raise
            last_error = exc
        if attempt < 2:
            time.sleep(2**attempt)
    raise YouTubeError(f"无法调用百炼 API：{last_error}") from last_error


def _download_json(uri: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        request = Request(uri, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise YouTubeError("百炼识别结果 JSON 顶层不是对象。")
            return value
        except (HTTPError, URLError, UnicodeDecodeError, json.JSONDecodeError, YouTubeError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code not in {429, 500, 502, 503, 504}:
                break
        if attempt < 2:
            time.sleep(2**attempt)
    raise YouTubeError(f"下载百炼识别结果失败：{last_error}") from last_error


def _format_milliseconds(value: Any) -> str:
    try:
        milliseconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        milliseconds = 0
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _parse_caption_timestamp(value: str) -> int:
    normalized = str(value or "").strip().replace(",", ".")
    parts = normalized.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            total_seconds = int(minutes) * 60 + float(seconds)
        elif len(parts) == 3:
            hours, minutes, seconds = parts
            total_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        else:
            raise ValueError
        return max(0, int(round(total_seconds * 1000)))
    except (TypeError, ValueError):
        raise YouTubeError(f"无法解析字幕时间戳：{value}") from None


def _clean_caption_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\{\\[^}]+\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.replace("\u200b", "").split()).strip()


def _parse_caption_blocks(raw_text: str) -> list[dict[str, Any]]:
    timestamp_pattern = re.compile(
        r"(?P<start>(?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
        r"(?P<end>(?:\d+:)?\d{2}:\d{2}[.,]\d{3})"
    )
    lines = str(raw_text or "").lstrip("\ufeff").splitlines()
    cues: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() == "WEBVTT":
            index += 1
            continue
        if line.upper().startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        match = timestamp_pattern.search(line)
        if not match:
            index += 1
            continue
        start = _parse_caption_timestamp(match.group("start"))
        end = _parse_caption_timestamp(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = _clean_caption_text(" ".join(text_lines))
        if not text:
            continue
        if cues and cues[-1]["text"] == text:
            continue
        cues.append({"begin_time": start, "end_time": max(start, end), "text": text})
    return cues


def _parse_json3_captions(raw_text: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise YouTubeError(f"无法解析 JSON3 字幕：{exc}") from exc
    events = value.get("events") if isinstance(value, dict) else None
    if not isinstance(events, list):
        return []
    cues: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        text = _clean_caption_text(
            "".join(
                str(segment.get("utf8") or "")
                for segment in segments
                if isinstance(segment, dict)
            )
        )
        if not text:
            continue
        try:
            start = max(0, int(event.get("tStartMs") or 0))
            end = start + max(0, int(event.get("dDurationMs") or 0))
        except (TypeError, ValueError):
            continue
        if cues and cues[-1]["text"] == text:
            continue
        cues.append({"begin_time": start, "end_time": end, "text": text})
    return cues


def parse_caption_content(raw_text: str, extension: str = "vtt") -> list[dict[str, Any]]:
    """Parse a downloaded VTT/SRT/JSON3 caption file into common timed cues."""

    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    if normalized_extension == "json3":
        return _parse_json3_captions(raw_text)
    return _parse_caption_blocks(raw_text)


def _decode_caption_bytes(value: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _relative_artifact_path(path: Path, artifact_dir: Path) -> str:
    try:
        return path.resolve().relative_to(artifact_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_caption_selection(
    selection_path: Path,
    *,
    status: str,
    reason: str | None,
    tracks: list[dict[str, Any]],
    selected: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    write_json(
        selection_path,
        {
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "available_tracks": [_public_caption_track(track) for track in tracks],
            "selected": _public_caption_track(selected) if selected else None,
            "error": error,
            "recorded_at": utc_now(),
        },
        sanitize=True,
    )


def write_caption_transcript_markdown(
    path: Path,
    transcription: dict[str, Any],
    *,
    language: str,
    caption_type: str,
    raw_caption_path: str,
) -> None:
    lines = [
        "# 视频文字稿",
        "",
        "- 来源：`YouTube 平台字幕`",
        f"- 字幕语言：`{language}`",
        f"- 字幕类型：`{caption_type}`",
        f"- 原始字幕：`{raw_caption_path}`",
        "",
        "## 逐字稿",
        "",
        *transcript_lines(transcription),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def caption_result_for_persist(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the normalized transcript before placing a caption result in JSON."""

    return {
        key: value
        for key, value in result.items()
        if key not in {"transcription"}
    }


def download_caption(
    source_url: str,
    artifact_dir: Path,
    *,
    language_preferences: Iterable[str] | None = None,
    allow_translated: bool = False,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> dict[str, Any]:
    """Try one original YouTube caption before any ASR submission.

    The returned result is safe to persist after passing through
    ``caption_result_for_persist``.  The selected signed caption URL exists only
    inside this function's process memory.
    """

    source_url = validate_video_url(source_url)
    artifact_dir = artifact_dir.expanduser().resolve()
    captions_dir = artifact_dir / "captions"
    captions_dir.mkdir(parents=True, exist_ok=True)
    selection_path = captions_dir / "selection.json"
    tracks: list[dict[str, Any]] = []
    try:
        tracks = get_caption_tracks(
            source_url,
            explicit_yt_dlp,
            cookies_from_browser,
            cookies_file,
        )
    except (OSError, subprocess.SubprocessError, YouTubeError, URLError) as exc:
        error = safe_error(str(exc), source_url)
        _write_caption_selection(
            selection_path,
            status="failed",
            reason="caption_metadata_failed",
            tracks=tracks,
            error=error,
        )
        return {
            "status": "failed",
            "reason": "caption_metadata_failed",
            "error": error,
            "available_tracks": [],
            "selection_path": _relative_artifact_path(selection_path, artifact_dir),
        }

    selected = _select_caption_track(
        tracks,
        language_preferences,
        allow_translated=allow_translated,
    )
    if selected is None:
        preferences = [
            str(item).strip()
            for item in (language_preferences or [])
            if str(item).strip()
        ]
        reason = "requested_language_not_found" if preferences else "no_usable_caption"
        _write_caption_selection(
            selection_path,
            status="skipped",
            reason=reason,
            tracks=tracks,
        )
        return {
            "status": "skipped",
            "reason": reason,
            "error": None,
            "available_tracks": [_public_caption_track(track) for track in tracks],
            "selection_path": _relative_artifact_path(selection_path, artifact_dir),
        }

    public_selected = _public_caption_track(selected)
    extension = str(selected.get("ext") or "vtt").lower().lstrip(".") or "vtt"
    raw_path = captions_dir / (
        f"{safe_component(str(selected.get('language') or 'und'), fallback='und', limit=32)}."
        f"{safe_component(str(selected.get('caption_type') or 'caption'), fallback='caption', limit=16)}."
        f"{safe_component(extension, fallback='vtt', limit=12)}"
    )
    raw_caption_url = str(selected.get("url") or "").strip()
    try:
        request = Request(
            raw_caption_url,
            headers={
                "Accept": "text/vtt,text/plain,application/json;q=0.9,*/*;q=0.1",
                "User-Agent": "Mozilla/5.0",
            },
            method="GET",
        )
        with urlopen(request, timeout=90) as response:
            raw_bytes = response.read()
        raw_path.write_bytes(raw_bytes)
        cues = parse_caption_content(_decode_caption_bytes(raw_bytes), extension)
        if not cues:
            raise YouTubeError("字幕文件下载成功，但解析后没有可读取的文字。")
        transcription = {
            "schema_version": 1,
            "source": "youtube-caption",
            "language": selected.get("language"),
            "caption_type": selected.get("caption_type"),
            "transcripts": [{"sentences": cues}],
        }
        relative_raw_path = _relative_artifact_path(raw_path, artifact_dir)
        transcript_path = artifact_dir / "transcript.md"
        write_caption_transcript_markdown(
            transcript_path,
            transcription,
            language=str(selected.get("language") or "und"),
            caption_type=str(selected.get("caption_type") or "manual"),
            raw_caption_path=relative_raw_path,
        )
        _write_caption_selection(
            selection_path,
            status="succeeded",
            reason=None,
            tracks=tracks,
            selected=selected,
        )
        return {
            "status": "succeeded",
            "reason": None,
            "error": None,
            "caption_language": str(selected.get("language") or "und"),
            "caption_type": str(selected.get("caption_type") or "manual"),
            "available_tracks": [_public_caption_track(track) for track in tracks],
            "selected": public_selected,
            "raw_path": relative_raw_path,
            "transcript_path": _relative_artifact_path(transcript_path, artifact_dir),
            "selection_path": _relative_artifact_path(selection_path, artifact_dir),
            "transcription": transcription,
        }
    except (HTTPError, OSError, UnicodeDecodeError, YouTubeError, URLError) as exc:
        reason = "caption_parse_failed" if raw_path.is_file() else "caption_download_failed"
        error = safe_error(str(exc), source_url, raw_caption_url)
        _write_caption_selection(
            selection_path,
            status="failed",
            reason=reason,
            tracks=tracks,
            selected=selected,
            error=error,
        )
        return {
            "status": "failed",
            "reason": reason,
            "error": error,
            "available_tracks": [_public_caption_track(track) for track in tracks],
            "selected": public_selected,
            "selection_path": _relative_artifact_path(selection_path, artifact_dir),
        }


BROWSER_TRANSCRIPT_CUE_PATTERN = re.compile(
    r"^\s*\[(?P<timestamp>\d{1,3}(?::\d{2}){1,2})\]\s*(?P<text>.+?)\s*$"
)


def _browser_caption_type(label: str) -> str:
    normalized = str(label or "").strip().lower()
    if "translated" in normalized or "翻译" in normalized:
        return "translated"
    if "auto" in normalized or "automatic" in normalized or "自动" in normalized:
        return "automatic"
    return "manual"


def parse_browser_transcript_content(
    raw_text: str,
    source_url: str,
) -> dict[str, Any]:
    """Parse the UTF-8 text exported by Computer Use from a YouTube page."""

    source_url = validate_video_url(source_url)
    lines = str(raw_text or "").lstrip("\ufeff").splitlines()
    headers: dict[str, str] = {}
    first_cue_index: int | None = None
    cues: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        match = re.match(r"^([^:\[\]]+):\s*(.*)$", line)
        if match and first_cue_index is None:
            headers[match.group(1).strip().lower()] = match.group(2).strip()
        cue_match = BROWSER_TRANSCRIPT_CUE_PATTERN.match(raw_line)
        if not cue_match:
            continue
        if first_cue_index is None:
            first_cue_index = index
        text = _clean_caption_text(cue_match.group("text"))
        if not text:
            continue
        start = _parse_caption_timestamp(cue_match.group("timestamp"))
        if cues and cues[-1]["text"] == text and cues[-1]["begin_time"] == start:
            continue
        cues.append({"begin_time": start, "end_time": start, "text": text})

    expected_video_id = video_id_from_url(source_url)
    exported_video_id = headers.get("video id", "").strip()
    if exported_video_id and exported_video_id != expected_video_id:
        raise YouTubeError(
            "浏览器导出的文字稿与当前 YouTube 链接不是同一个视频。"
        )
    if not cues:
        raise YouTubeError("浏览器导出的 YouTube 文字稿中没有可读取的时间轴句子。")

    for index, cue in enumerate(cues[:-1]):
        cue["end_time"] = max(cue["begin_time"], cues[index + 1]["begin_time"])
    language = headers.get("language", "").strip() or "und"
    caption_type = _browser_caption_type(headers.get("captions", ""))
    return {
        "video_id": exported_video_id or expected_video_id,
        "language": language,
        "caption_type": caption_type,
        "transcription": {
            "schema_version": 1,
            "source": "youtube-browser-caption",
            "language": language,
            "caption_type": caption_type,
            "transcripts": [{"sentences": cues}],
        },
    }


def import_browser_transcript(
    source_url: str,
    transcript_file: Path,
    artifact_dir: Path,
    *,
    title: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Import a Computer Use YouTube transcript into the normal note contract."""

    source_url = validate_video_url(source_url)
    transcript_file = transcript_file.expanduser().resolve()
    artifact_dir = artifact_dir.expanduser().resolve()
    try:
        raw_bytes = transcript_file.read_bytes()
    except OSError as exc:
        raise YouTubeError(f"无法读取浏览器导出的文字稿：{transcript_file}\n{exc}") from exc
    parsed = parse_browser_transcript_content(
        _decode_caption_bytes(raw_bytes),
        source_url,
    )
    language = str(parsed["language"] or "und")
    caption_type = str(parsed["caption_type"] or "manual")
    metadata = clean_video_metadata(
        {
            "id": parsed["video_id"],
            "title": str(title or parsed["video_id"]).strip(),
            "channel": str(author or "").strip(),
        },
        source_url,
    )
    captions_dir = artifact_dir / "captions"
    captions_dir.mkdir(parents=True, exist_ok=True)
    raw_path = captions_dir / (
        f"{safe_component(language, fallback='und', limit=32)}."
        f"{safe_component(caption_type, fallback='caption', limit=16)}.browser.txt"
    )
    raw_path.write_bytes(raw_bytes)
    transcript_path = artifact_dir / "transcript.md"
    relative_raw_path = _relative_artifact_path(raw_path, artifact_dir)
    write_caption_transcript_markdown(
        transcript_path,
        parsed["transcription"],
        language=language,
        caption_type=caption_type,
        raw_caption_path=relative_raw_path,
    )
    selected = {"language": language, "caption_type": caption_type, "ext": "txt"}
    selection_path = captions_dir / "selection.json"
    _write_caption_selection(
        selection_path,
        status="succeeded",
        reason=None,
        tracks=[selected],
        selected=selected,
    )
    public_selected = _public_caption_track(selected)
    return {
        "status": "succeeded",
        "reason": None,
        "error": None,
        "caption_language": language,
        "caption_type": caption_type,
        "available_tracks": [public_selected],
        "selected": public_selected,
        "raw_path": relative_raw_path,
        "transcript_path": _relative_artifact_path(transcript_path, artifact_dir),
        "selection_path": _relative_artifact_path(selection_path, artifact_dir),
        "retrieval_method": "computer_use",
        "transcription": parsed["transcription"],
        "metadata": metadata,
    }


def transcript_has_content(transcription: dict[str, Any]) -> bool:
    """Return whether a Bailian result contains actual transcript text."""

    raw_transcripts = transcription.get("transcripts")
    transcripts = raw_transcripts if isinstance(raw_transcripts, list) else []
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        raw_sentences = transcript.get("sentences")
        sentences = raw_sentences if isinstance(raw_sentences, list) else []
        if any(
            isinstance(sentence, dict) and str(sentence.get("text") or "").strip()
            for sentence in sentences
        ):
            return True
        if str(transcript.get("text") or "").strip():
            return True
    return False


def transcript_lines(transcription: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    found_sentence = False
    raw_transcripts = transcription.get("transcripts")
    transcripts = raw_transcripts if isinstance(raw_transcripts, list) else []

    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        raw_sentences = transcript.get("sentences")
        sentences = raw_sentences if isinstance(raw_sentences, list) else []
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            text = str(sentence.get("text") or "").strip()
            if not text:
                continue
            found_sentence = True
            start = _format_milliseconds(sentence.get("begin_time"))
            end = _format_milliseconds(sentence.get("end_time"))
            speaker = ""
            if sentence.get("speaker_id") is not None:
                speaker = f"说话人 {sentence.get('speaker_id')}："
            lines.append(f"[{start} - {end}] {speaker}{text}")

    if not found_sentence:
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            text = str(transcript.get("text") or "").strip()
            if text:
                lines.append(text)
                found_sentence = True

    if not found_sentence:
        lines.append("> 百炼返回结果中没有可读取的句子。")
    return lines


def write_transcript_markdown(
    path: Path,
    transcription: dict[str, Any],
    model: str,
    task_id: str,
) -> None:
    lines = [
        "# 视频文字稿",
        "",
        f"- 模型：`{model}`",
        f"- Task ID：`{task_id}`",
        "",
        "## 逐字稿",
        "",
        *transcript_lines(transcription),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _first_successful_result(task: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    output = task.get("output")
    output = output if isinstance(output, dict) else {}
    raw_results = output.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("subtask_status") or "").upper() != "SUCCEEDED":
            continue
        transcription_url = str(item.get("transcription_url") or "").strip()
        if transcription_url:
            return transcription_url, item
    raise YouTubeError("百炼任务成功但没有返回可下载的 transcription_url。")


def _notify_stage(
    callback: StageCallback | None,
    stage: str,
    status: str,
    *,
    artifact_paths: Iterable[str] = (),
    error: str | None = None,
    retryable: bool | None = None,
    reason: str | None = None,
) -> None:
    if callback is None:
        return
    callback(
        stage,
        status,
        artifact_paths=list(artifact_paths),
        error=error,
        retryable=retryable,
        reason=reason,
    )


def transcribe_media(
    source_url: str,
    artifact_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    api_base_url: str = DEFAULT_API_BASE_URL,
    poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    language_hints: Iterable[str] | None = None,
    diarization: bool = False,
    media_path: Path | None = None,
    oss_object_key: str | None = None,
    oss_url_expires: int = 3600,
    resume: bool = False,
    on_stage: StageCallback | None = None,
) -> dict[str, Any]:
    """Deliver local media to Bailian, or explicitly resume a saved ASR task."""

    source_url = validate_video_url(source_url)
    if poll_interval <= 0 or timeout <= 0:
        raise YouTubeError("轮询间隔和超时时间必须是正整数。")
    if oss_url_expires <= 0:
        raise YouTubeError("OSS 签名 URL 有效期必须是正整数秒数。")
    artifact_dir = artifact_dir.expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    api_base_url = api_base_url.rstrip("/")
    model = str(model).strip() or DEFAULT_MODEL
    delivery = "oss-signed-url"
    checkpoint = _load_asr_checkpoint(artifact_dir) if resume else {}
    checkpoint_task_id = str(checkpoint.get("task_id") or "").strip()
    checkpoint_task = checkpoint.get("task")
    checkpoint_transcription = checkpoint.get("transcription")
    attempt = int(checkpoint.get("attempt") or 0)
    transcript_path = artifact_dir / "transcript.md"

    # A completed local result is already the strongest checkpoint. Re-rendering
    # it, if necessary, must not require a new API request or another API key.
    if (
        resume
        and checkpoint_task_id
        and isinstance(checkpoint_transcription, dict)
        and transcript_has_content(checkpoint_transcription)
    ):
        request_info = checkpoint.get("request_info")
        request_info = request_info if isinstance(request_info, dict) else {}
        checkpoint_model = str(request_info.get("model") or model).strip() or model
        if not transcript_path.is_file():
            _notify_stage(
                on_stage,
                "asr_submit",
                "succeeded",
                artifact_paths=("submit.json",),
                reason="checkpoint_reused",
            )
            _notify_stage(
                on_stage,
                "asr_poll",
                "succeeded",
                artifact_paths=("task.json",),
                reason="checkpoint_reused",
            )
            _notify_stage(
                on_stage,
                "transcript_download",
                "succeeded",
                artifact_paths=("transcription.json",),
                reason="checkpoint_reused",
            )
            _notify_stage(on_stage, "markdown_render", "running", reason="checkpoint_reused")
            write_transcript_markdown(
                transcript_path,
                checkpoint_transcription,
                checkpoint_model,
                checkpoint_task_id,
            )
            _notify_stage(
                on_stage,
                "markdown_render",
                "succeeded",
                artifact_paths=("transcript.md",),
                reason="checkpoint_reused",
            )
        request_info.update(
            {
                "schema_version": 1,
                "status": "completed",
                "model": checkpoint_model,
                "source_url": source_url,
                "api_base_url": api_base_url,
                "delivery": request_info.get("delivery") or delivery,
                "attempt": max(1, attempt),
                "task_id": checkpoint_task_id,
                "resumed_at": utc_now(),
                "completed_at": utc_now(),
            }
        )
        write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
        return {
            "status": "completed",
            "model": checkpoint_model,
            "task_id": checkpoint_task_id,
            "delivery": request_info.get("delivery") or delivery,
            "resolved_host": str(request_info.get("resolved_host") or ""),
            "oss_object_key": request_info.get("oss_object_key"),
            "transcript_path": str(transcript_path),
            "transcription": checkpoint_transcription,
            "result": {},
            "resumed": True,
        }

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise YouTubeError(
            "DASHSCOPE_API_KEY 未配置。请在当前 PowerShell 会话设置环境变量后重试。"
        )

    task_id = checkpoint_task_id if resume else ""
    task = checkpoint_task if isinstance(checkpoint_task, dict) else None
    transcription = checkpoint_transcription if isinstance(checkpoint_transcription, dict) else None
    result_item: dict[str, Any] = {}
    signed_url = ""
    oss_info: dict[str, Any] = {}
    active_stage: str | None = None
    attempt_initialized = False
    reused_checkpoint_task = False

    def new_request_info(current_attempt: int) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "running",
            "model": model,
            "source_url": source_url,
            "api_base_url": api_base_url,
            "delivery": delivery,
            "attempt": current_attempt,
            "submitted_at": utc_now(),
        }

    checkpoint_request_info = checkpoint.get("request_info")
    request_info: dict[str, Any] = (
        dict(checkpoint_request_info)
        if isinstance(checkpoint_request_info, dict)
        else new_request_info(max(1, attempt or 1))
    )

    try:
        if resume and task_id:
            task_status = _task_status(task)
            if task_status not in FINAL_STATUSES:
                task = _api_json("GET", f"{api_base_url}/tasks/{task_id}", api_key)
                task_status = _task_status(task)
                write_json(artifact_dir / "task.json", task, sanitize=True)

            if task_status in {"FAILED", "UNKNOWN"}:
                _notify_stage(
                    on_stage,
                    "asr_poll",
                    "failed",
                    artifact_paths=("task.json",) if task else (),
                    error=f"已有百炼任务状态为 {task_status}，准备创建新的 attempt。",
                    retryable=True,
                    reason="checkpoint_task_failed",
                )
                previous_attempt = max(1, attempt)
                _archive_current_attempt(
                    artifact_dir,
                    previous_attempt,
                    task_id=task_id,
                    task_status=task_status,
                )
                _clear_active_attempt(artifact_dir)
                attempt = previous_attempt + 1
                task_id = ""
                task = None
                transcription = None
                request_info = new_request_info(attempt)
                write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
                attempt_initialized = True
            else:
                checkpoint_model = str(
                    (checkpoint.get("request_info") or {}).get("model") or model
                ).strip()
                if checkpoint_model:
                    model = checkpoint_model
                request_info = checkpoint.get("request_info")
                request_info = request_info if isinstance(request_info, dict) else {}
                request_info.update(
                    {
                        "schema_version": 1,
                        "status": "running",
                        "model": model,
                        "source_url": source_url,
                        "api_base_url": api_base_url,
                        "delivery": delivery,
                        "attempt": max(1, attempt),
                        "task_id": task_id,
                        "resumed_at": utc_now(),
                    }
                )
                write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
                _notify_stage(
                    on_stage,
                    "asr_submit",
                    "succeeded",
                    artifact_paths=("submit.json",),
                    reason="resumed_existing_task",
                )
                reused_checkpoint_task = True

        if not task_id:
            if resume and not attempt_initialized and attempt > 0 and any(
                (artifact_dir / name).is_file() for name in ASR_CHECKPOINT_FILES
            ):
                previous_attempt = max(1, attempt)
                _archive_current_attempt(
                    artifact_dir,
                    previous_attempt,
                    task_id=None,
                    task_status=str(checkpoint.get("task_status") or "") or None,
                )
                _clear_active_attempt(artifact_dir)
                attempt = previous_attempt + 1
                attempt_initialized = True
            else:
                attempt = max(1, attempt)
            request_info = new_request_info(attempt)
            write_json(artifact_dir / "request-info.json", request_info, sanitize=True)

            if media_path is None:
                raise YouTubeError("创建新的 ASR 任务需要本地媒体文件，但没有提供 media_path。")
            media_path = Path(media_path).expanduser().resolve()
            if not media_path.is_file():
                raise YouTubeError(f"找不到待上传的本地媒体文件：{media_path}")

            default_key = (
                f"youtube-asr/{safe_component(video_id_from_url(source_url), fallback='video')}"
                f"{media_path.suffix.lower() or '.bin'}"
            )
            active_stage = "oss_upload"
            _notify_stage(on_stage, active_stage, "running")
            oss_info = upload_local_media_to_oss(
                media_path,
                oss_object_key or default_key,
                signed_url_expires=oss_url_expires,
            )
            signed_url = str(oss_info["signed_url"])
            write_json(
                artifact_dir / "oss.json",
                {
                    "schema_version": 1,
                    "status": "uploaded",
                    "bucket": oss_info["bucket"],
                    "object_key": oss_info["object_key"],
                    "local_path": oss_info["local_path"],
                    "size_bytes": oss_info["size_bytes"],
                    "content_type": oss_info["content_type"],
                    "signed_url_expires": oss_info["signed_url_expires"],
                    "uploaded_at": oss_info["uploaded_at"],
                },
                sanitize=True,
            )
            _notify_stage(on_stage, active_stage, "succeeded", artifact_paths=("oss.json",))

            parameters: dict[str, Any] = {"channel_id": [0]}
            hints = [str(item).strip() for item in (language_hints or []) if str(item).strip()]
            if hints:
                parameters["language_hints"] = hints[:4]
            if diarization:
                parameters["diarization_enabled"] = True
            submit_body = {
                "model": model,
                "input": {"file_urls": [signed_url]},
                "parameters": parameters,
            }
            active_stage = "asr_submit"
            _notify_stage(on_stage, active_stage, "running")
            submit = _api_json(
                "POST",
                f"{api_base_url}/services/audio/asr/transcription",
                api_key,
                submit_body,
                asynchronous=True,
            )
            write_json(artifact_dir / "submit.json", submit, sanitize=True)
            task_id = _task_id_from_payload(submit)
            if not task_id:
                raise YouTubeError(f"提交成功响应中没有 task_id；详见 {artifact_dir / 'submit.json'}")
            # This write is deliberately immediately after parsing task_id. If
            # the process stops before the first poll, the next run still knows
            # which billable task to query and will not submit another one.
            request_info.update(
                {
                    "status": "submitted",
                    "task_id": task_id,
                    "attempt": attempt,
                    "submitted_at": utc_now(),
                }
            )
            write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
            _notify_stage(on_stage, active_stage, "succeeded", artifact_paths=("submit.json",))
            task = None
            transcription = None
        else:
            # The existing task was found in a checkpoint, so its signed OSS
            # URL has expired or is irrelevant. Never upload media again here.
            active_stage = "asr_poll"
            _notify_stage(on_stage, active_stage, "running", reason="resuming_existing_task")

        active_stage = "asr_poll"
        if task is None or _task_status(task) not in FINAL_STATUSES:
            deadline = time.monotonic() + timeout
            last_status = ""
            while True:
                task = _api_json("GET", f"{api_base_url}/tasks/{task_id}", api_key)
                task_status = _task_status(task) or "UNKNOWN"
                write_json(artifact_dir / "task.json", task, sanitize=True)
                if task_status != last_status:
                    print(f"[youtube-asr] status: {task_status}", flush=True)
                    last_status = task_status
                if task_status in FINAL_STATUSES:
                    break
                if time.monotonic() > deadline:
                    raise YouTubeError(f"轮询超过 {timeout} 秒；task_id={task_id}。")
                time.sleep(poll_interval)
        else:
            task_status = _task_status(task)
            # A persisted task response has redacted its result URL. Refresh
            # successful tasks when the result JSON is not already local.
            if task_status == "SUCCEEDED" and not (
                isinstance(transcription, dict) and transcript_has_content(transcription)
            ):
                task = _api_json("GET", f"{api_base_url}/tasks/{task_id}", api_key)
                task_status = _task_status(task) or "UNKNOWN"
                write_json(artifact_dir / "task.json", task, sanitize=True)

        task_status = _task_status(task) or "UNKNOWN"
        if task_status != "SUCCEEDED":
            raise YouTubeError(f"百炼转写失败（状态：{task_status}）；详见 {artifact_dir / 'task.json'}")
        _notify_stage(on_stage, active_stage, "succeeded", artifact_paths=("task.json",))

        active_stage = "transcript_download"
        if isinstance(transcription, dict) and transcript_has_content(transcription):
            try:
                _, result_item = _first_successful_result(task)
            except YouTubeError:
                result_item = {}
            _notify_stage(
                on_stage,
                active_stage,
                "succeeded",
                artifact_paths=("transcription.json",),
                reason="checkpoint_reused",
            )
        else:
            _notify_stage(on_stage, active_stage, "running")
            transcription_url, result_item = _first_successful_result(task)
            transcription = _download_json(transcription_url)
            write_json(artifact_dir / "transcription.json", transcription, sanitize=True)
            if not transcript_has_content(transcription):
                raise YouTubeError(
                    f"百炼任务完成但没有返回可读文字稿；详见 {artifact_dir / 'transcription.json'}"
                )
            _notify_stage(
                on_stage,
                active_stage,
                "succeeded",
                artifact_paths=("transcription.json",),
            )

        active_stage = "markdown_render"
        _notify_stage(on_stage, active_stage, "running")
        write_transcript_markdown(transcript_path, transcription, model, task_id)
        _notify_stage(on_stage, active_stage, "succeeded", artifact_paths=("transcript.md",))
        active_stage = None

        request_info.update(
            {
                "status": "completed",
                "task_id": task_id,
                "delivery": delivery,
                "attempt": attempt,
                "resolved_host": urlparse(signed_url).netloc
                if signed_url
                else request_info.get("resolved_host"),
                "oss_object_key": oss_info.get("object_key") or request_info.get("oss_object_key"),
                "completed_at": utc_now(),
            }
        )
        write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
        return {
            "status": "completed",
            "model": model,
            "task_id": task_id,
            "delivery": delivery,
            "resolved_host": urlparse(signed_url).netloc
            if signed_url
            else str(request_info.get("resolved_host") or ""),
            "oss_object_key": oss_info.get("object_key") or request_info.get("oss_object_key"),
            "transcript_path": str(transcript_path),
            "transcription": transcription,
            "result": sanitize_for_persist(result_item),
            "resumed": reused_checkpoint_task,
        }
    except KeyboardInterrupt:
        message = "用户中断了视频转写。"
        if active_stage:
            _notify_stage(
                on_stage,
                active_stage,
                "cancelled",
                error=message,
                retryable=True,
            )
        write_json(
            artifact_dir / "error.json",
            {
                "status": "cancelled",
                "error": message,
                "task_id": task_id or None,
                "failed_stage": active_stage,
                "recorded_at": utc_now(),
            },
        )
        request_info.update(
            {
                "status": "cancelled",
                "task_id": task_id or None,
                "error": message,
                "cancelled_at": utc_now(),
            }
        )
        write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
        raise
    except (OSError, subprocess.SubprocessError, YouTubeError, URLError) as exc:
        message = safe_error(str(exc), source_url, signed_url)
        if active_stage:
            _notify_stage(
                on_stage,
                active_stage,
                "failed",
                error=message,
                retryable=active_stage != "asr_submit",
            )
        write_json(
            artifact_dir / "error.json",
            {
                "status": "failed",
                "error": message,
                "task_id": task_id or None,
                "failed_stage": active_stage,
                "recorded_at": utc_now(),
            },
        )
        request_info.update(
            {
                "status": "failed",
                "task_id": task_id or None,
                "error": message,
                "failed_at": utc_now(),
            }
        )
        write_json(artifact_dir / "request-info.json", request_info, sanitize=True)
        raise YouTubeError(message) from exc


def _frontmatter_value(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _frontmatter_nullable(value: Any) -> str:
    if value is None or value == "":
        return "null"
    return _frontmatter_value(value)


def write_video_note(
    path: Path,
    metadata: dict[str, Any],
    *,
    scope: str,
    status: str,
    transcript_source: str | None,
    caption_language: str | None = None,
    caption_type: str | None = None,
    asr_status: str | None = None,
    media_delivery: str | None = None,
    transcript: dict[str, Any] | None = None,
    asr_model: str | None = None,
    task_id: str | None = None,
    transcript_path: str | None = None,
    caption_path: str | None = None,
    error: str | None = None,
    review_status: str = "unreviewed",
) -> None:
    """Write the user-facing Markdown note without any signed media URL."""

    contract = build_transcript_contract(
        metadata,
        scope=scope,
        status=status,
        transcript_source=transcript_source,
        caption_language=caption_language,
        caption_type=caption_type,
        asr_status=asr_status,
        asr_model=asr_model,
        task_id=task_id,
        media_delivery=media_delivery,
        review_status=review_status,
    )
    title = " ".join(str(contract["title"]).split())
    lines = [
        "---",
        f"contract_version: {_frontmatter_value(contract['contract_version'])}",
        f"platform: {_frontmatter_value(contract['platform'])}",
        f"source_url: {_frontmatter_value(contract['source_url'])}",
        f"source_id: {_frontmatter_value(contract['source_id'])}",
        f"author: {_frontmatter_value(contract['author'])}",
        f"channel_url: {_frontmatter_nullable(contract['channel_url'])}",
        f"title: {_frontmatter_value(title)}",
        f"published_at: {_frontmatter_value(contract['published_at'])}",
        f"captured_at: {_frontmatter_value(contract['captured_at'])}",
        f"scope: {_frontmatter_value(contract['scope'])}",
        f"transcript_source: {_frontmatter_nullable(contract['transcript_source'])}",
        f"caption_language: {_frontmatter_nullable(contract['caption_language'])}",
        f"caption_type: {_frontmatter_nullable(contract['caption_type'])}",
        f"asr_status: {_frontmatter_value(contract['asr_status'])}",
        f"asr_model: {_frontmatter_nullable(contract['asr_model'])}",
        f"task_id: {_frontmatter_nullable(contract['task_id'])}",
        f"media_delivery: {_frontmatter_nullable(contract['media_delivery'])}",
        f"status: {_frontmatter_value(contract['status'])}",
        f"review_status: {_frontmatter_value(contract['review_status'])}",
        f"duration_seconds: {_frontmatter_nullable(contract['duration_seconds'])}",
        f"transcript_path: {_frontmatter_nullable(transcript_path)}",
        "---",
        "",
        f"# {title}",
        "",
        f"- 原始视频：{contract['source_url'] or 'unknown'}",
        f"- 作者／频道：{contract['author'] or 'unknown'}",
        f"- 发布时间：{contract['published_at'] or 'unknown'}",
        f"- 采集状态：`{contract['status']}`",
        "",
    ]
    if error:
        lines.extend(
            [
                "## 处理说明",
                "",
                "> 文字稿未完成，原始链接和处理错误已保留，不能把此文件当作完整文字稿。",
                "",
                f"> 错误：{error}",
                "",
            ]
        )
    elif transcript is not None:
        lines.extend(["## 视频文字稿", "", *transcript_lines(transcript), ""])
        if transcript_source == "platform_caption":
            source_text = "YouTube 平台字幕"
        elif media_delivery == "oss-signed-url":
            source_text = "本地音频上传到私有 OSS 后生成的短时签名 URL"
        else:
            source_text = "百炼语音识别"
        lines.extend(
            [
                "## 处理说明",
                "",
                (
                    f"> 文字稿来自{source_text}。"
                    if transcript_source == "platform_caption"
                    else f"> 文字稿由{source_text}交给百炼异步语音识别生成。"
                ),
                (
                    f"> 原始字幕文件：`{caption_path}`；本次未提交 ASR。"
                    if transcript_source == "platform_caption" and caption_path
                    else "> OSS 签名 URL 未写入此 Markdown；请以原始视频链接作为来源。"
                ),
                "",
            ]
        )
    else:
        lines.extend(["> 没有可读取的文字稿。", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def safe_component(value: str, fallback: str = "item", limit: int = 100) -> str:
    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:limit].rstrip(" .") or fallback


def metadata_from_flat_item(item: dict[str, Any], source_url: str) -> dict[str, Any]:
    """Normalize channel-list metadata when a full metadata call is unavailable."""

    return clean_video_metadata(item, source_url)
