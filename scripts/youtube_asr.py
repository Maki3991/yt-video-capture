#!/usr/bin/env python3
"""Shared YouTube metadata, yt-dlp and Bailian transcription helpers.

The module deliberately keeps the first YouTube implementation independent from
the XHS Skill.  Both Skills use the same evidence contract, while their page
collection steps remain separate.  The primary media path is:

    YouTube page -> yt-dlp temporary media URL -> Bailian async ASR -> Markdown

Temporary signed URLs are only held in memory long enough to submit/download a
task.  Persisted JSON diagnostics redact their query values.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


DEFAULT_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DEFAULT_API_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 7200
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
    "transcription_url",
}


class YouTubeError(RuntimeError):
    """A user-facing, non-secret error from the YouTube pipeline."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def safe_error(value: object, source_url: str = "", direct_url: str = "") -> str:
    message = str(value).strip()
    if source_url:
        message = message.replace(source_url, "<source-url>")
    if direct_url:
        message = message.replace(direct_url, "<resolved-media-url>")
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


def _run_yt_dlp(
    args: list[str],
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [*_find_yt_dlp(explicit_yt_dlp), "--ignore-config"]
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
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
        }
    )
    return sanitize_for_persist(result)


def get_video_metadata(
    source_url: str,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
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
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or "").strip()
        raise YouTubeError(
            "yt-dlp 无法读取视频元数据。\n"
            + safe_error(diagnostic[-2000:] or f"return code {completed.returncode}", source_url)
        )
    raw = _parse_json_stdout(completed.stdout)
    return clean_video_metadata(raw, source_url)


def enumerate_channel(
    channel_url: str,
    limit: int,
    *,
    tab: str = "videos",
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
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


def resolve_direct_url(
    source_url: str,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
) -> tuple[str, str]:
    """Resolve a temporary audio/video URL without downloading locally."""

    source_url = validate_video_url(source_url)
    yt_dlp = _find_yt_dlp(explicit_yt_dlp)
    common = [*yt_dlp, "--ignore-config"]
    if cookies_from_browser:
        common.extend(["--cookies-from-browser", cookies_from_browser])
    common.extend(
        [
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--retries",
            "3",
            "--socket-timeout",
            "30",
            "-g",
        ]
    )
    selectors = (
        "bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best",
        "best[ext=mp4]/best",
    )
    diagnostics: list[str] = []
    for selector in selectors:
        completed = subprocess.run(
            [*common, "-f", selector, source_url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            check=False,
        )
        urls = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        if completed.returncode == 0 and urls:
            return urls[0], " ".join(yt_dlp)
        diagnostics.append(
            f"format {selector}: "
            f"{safe_error((completed.stderr or '').strip()[-1000:], source_url)}"
        )
    raise YouTubeError("yt-dlp 没有返回可交给百炼的媒体直链。\n" + "\n".join(diagnostics))


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


def transcribe_media(
    source_url: str,
    artifact_dir: Path,
    *,
    explicit_yt_dlp: str | None = None,
    cookies_from_browser: str | None = None,
    model: str = DEFAULT_MODEL,
    api_base_url: str = DEFAULT_API_BASE_URL,
    poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    language_hints: Iterable[str] | None = None,
    diarization: bool = False,
) -> dict[str, Any]:
    """Resolve, submit, poll and save one ASR result."""

    source_url = validate_video_url(source_url)
    if poll_interval <= 0 or timeout <= 0:
        raise YouTubeError("轮询间隔和超时时间必须是正整数。")
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise YouTubeError(
            "DASHSCOPE_API_KEY 未配置。请在当前 PowerShell 会话设置环境变量后重试。"
        )

    artifact_dir = artifact_dir.expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    api_base_url = api_base_url.rstrip("/")
    model = str(model).strip() or DEFAULT_MODEL
    request_info = {
        "schema_version": 1,
        "status": "running",
        "model": model,
        "source_url": source_url,
        "api_base_url": api_base_url,
        "submitted_at": utc_now(),
    }
    write_json(artifact_dir / "request-info.json", request_info)

    direct_url = ""
    task_id = ""
    try:
        direct_url, yt_dlp_description = resolve_direct_url(
            source_url,
            explicit_yt_dlp,
            cookies_from_browser,
        )
        write_json(
            artifact_dir / "resolution.json",
            {
                "schema_version": 1,
                "status": "resolved",
                "source_url": source_url,
                "resolved_url": direct_url,
                "resolved_host": urlparse(direct_url).netloc,
                "yt_dlp": yt_dlp_description,
                "auth_mode": "cookies-from-browser" if cookies_from_browser else "none",
                "resolved_at": utc_now(),
            },
            sanitize=True,
        )

        parameters: dict[str, Any] = {"channel_id": [0]}
        hints = [str(item).strip() for item in (language_hints or []) if str(item).strip()]
        if hints:
            parameters["language_hints"] = hints[:4]
        if diarization:
            parameters["diarization_enabled"] = True
        submit_body = {
            "model": model,
            "input": {"file_urls": [direct_url]},
            "parameters": parameters,
        }
        submit = _api_json(
            "POST",
            f"{api_base_url}/services/audio/asr/transcription",
            api_key,
            submit_body,
            asynchronous=True,
        )
        write_json(artifact_dir / "submit.json", submit, sanitize=True)
        submit_output = submit.get("output")
        submit_output = submit_output if isinstance(submit_output, dict) else {}
        task_id = str(submit_output.get("task_id") or "").strip()
        if not task_id:
            raise YouTubeError(f"提交成功响应中没有 task_id；详见 {artifact_dir / 'submit.json'}")

        deadline = time.monotonic() + timeout
        task: dict[str, Any] | None = None
        last_status = ""
        while True:
            task = _api_json("GET", f"{api_base_url}/tasks/{task_id}", api_key)
            task_output = task.get("output")
            task_output = task_output if isinstance(task_output, dict) else {}
            status = str(task_output.get("task_status") or "UNKNOWN").upper()
            if status != last_status:
                print(f"[youtube-asr] status: {status}", flush=True)
                last_status = status
            if status in FINAL_STATUSES:
                break
            if time.monotonic() > deadline:
                raise YouTubeError(f"轮询超过 {timeout} 秒；task_id={task_id}。")
            time.sleep(poll_interval)

        write_json(artifact_dir / "task.json", task, sanitize=True)
        task_output = task.get("output")
        task_output = task_output if isinstance(task_output, dict) else {}
        if str(task_output.get("task_status") or "").upper() != "SUCCEEDED":
            raise YouTubeError(f"百炼转写失败；详见 {artifact_dir / 'task.json'}")

        transcription_url, result_item = _first_successful_result(task)
        transcription = _download_json(transcription_url)
        write_json(artifact_dir / "transcription.json", transcription, sanitize=True)
        transcript_path = artifact_dir / "transcript.md"
        write_transcript_markdown(transcript_path, transcription, model, task_id)

        request_info.update(
            {
                "status": "completed",
                "task_id": task_id,
                "resolved_host": urlparse(direct_url).netloc,
                "completed_at": utc_now(),
            }
        )
        write_json(artifact_dir / "request-info.json", request_info)
        return {
            "status": "completed",
            "model": model,
            "task_id": task_id,
            "resolved_host": urlparse(direct_url).netloc,
            "transcript_path": str(transcript_path),
            "transcription": transcription,
            "result": sanitize_for_persist(result_item),
        }
    except KeyboardInterrupt:
        status = "cancelled"
        message = "用户中断了视频转写。"
        raise YouTubeError(message) from None
    except (OSError, subprocess.SubprocessError, YouTubeError, URLError) as exc:
        message = safe_error(str(exc), source_url, direct_url)
        write_json(
            artifact_dir / "error.json",
            {
                "status": "failed",
                "error": message,
                "task_id": task_id or None,
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
        write_json(artifact_dir / "request-info.json", request_info)
        raise YouTubeError(message) from exc


def _frontmatter_value(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def write_video_note(
    path: Path,
    metadata: dict[str, Any],
    *,
    scope: str,
    status: str,
    transcript_source: str,
    transcript: dict[str, Any] | None = None,
    asr_model: str | None = None,
    task_id: str | None = None,
    transcript_path: str | None = None,
    error: str | None = None,
) -> None:
    """Write the user-facing Markdown note without any signed media URL."""

    title = " ".join(
        str(metadata.get("title") or metadata.get("source_id") or "未命名视频")
        .split()
    )
    lines = [
        "---",
        f"platform: {_frontmatter_value('youtube')}",
        f"source_url: {_frontmatter_value(metadata.get('source_url'))}",
        f"source_id: {_frontmatter_value(metadata.get('source_id'))}",
        f"author: {_frontmatter_value(metadata.get('author'))}",
        f"channel_url: {_frontmatter_value(metadata.get('channel_url'))}",
        f"title: {_frontmatter_value(title)}",
        f"published_at: {_frontmatter_value(metadata.get('published_at'))}",
        f"captured_at: {_frontmatter_value(utc_now())}",
        f"scope: {_frontmatter_value(scope)}",
        f"transcript_source: {_frontmatter_value(transcript_source)}",
        f"status: {_frontmatter_value(status)}",
        f"duration_seconds: {_frontmatter_value(metadata.get('duration'))}",
        f"asr_model: {_frontmatter_value(asr_model)}",
        f"task_id: {_frontmatter_value(task_id)}",
        f"transcript_path: {_frontmatter_value(transcript_path)}",
        "---",
        "",
        f"# {title}",
        "",
        f"- 原始视频：{metadata.get('source_url') or 'unknown'}",
        f"- 作者／频道：{metadata.get('author') or 'unknown'}",
        f"- 发布时间：{metadata.get('published_at') or 'unknown'}",
        f"- 采集状态：`{status}`",
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
        lines.extend(
            [
                "## 处理说明",
                "",
                "> 文字稿由 yt-dlp 解析出的临时媒体直链交给百炼异步语音识别生成。",
                "> 临时签名直链未写入此 Markdown；请以原始视频链接作为来源。",
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
