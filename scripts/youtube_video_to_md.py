#!/usr/bin/env python3
"""Convert one YouTube video into a traceable Markdown transcript."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from youtube_asr import (
    DEFAULT_API_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    YouTubeError,
    clean_video_metadata,
    download_audio,
    get_video_metadata,
    read_json,
    safe_component,
    transcribe_media,
    utc_now,
    video_id_from_url,
    validate_video_url,
    write_json,
    write_video_note,
)


def _parse_language_hints(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_output(source_url: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    source_id = safe_component(source_url.rsplit("/", 1)[-1], fallback="video", limit=24)
    return Path("youtube-video-results") / f"{stamp}-{source_id}"


def _base_metadata(source_url: str) -> dict[str, Any]:
    return clean_video_metadata({}, source_url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将一个 YouTube 视频用 yt-dlp + 百炼 ASR 保存为 Markdown。"
    )
    parser.add_argument("source", help="YouTube 单个视频链接")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="输出目录；未指定时使用 ./youtube-video-results/<时间>-<视频标识>",
    )
    parser.add_argument(
        "--yt-dlp",
        default=None,
        help="yt-dlp 可执行文件路径；未指定时使用 YOUTUBE_YTDLP、PATH 或 Python 模块",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        metavar="BROWSER[:PROFILE]",
        help="显式允许 yt-dlp 从浏览器读取 YouTube Cookie；默认不读取",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"百炼模型；默认 {DEFAULT_MODEL}")
    parser.add_argument(
        "--api-base-url",
        default=None,
        help=f"百炼 API 根地址；默认 DASHSCOPE_API_BASE_URL 或 {DEFAULT_API_BASE_URL}",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="异步任务轮询间隔秒数；默认 5",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="最长等待秒数；默认 7200",
    )
    parser.add_argument(
        "--language-hints",
        default=None,
        help="可选语言提示，逗号分隔，例如 zh,en；不填则由模型自动判断",
    )
    parser.add_argument(
        "--diarization",
        action="store_true",
        help="请求说话人分离；长视频建议谨慎使用",
    )
    parser.add_argument(
        "--via-oss",
        action="store_true",
        help="先使用本地音频，再上传私有 OSS，通过短时签名 URL 交给百炼",
    )
    parser.add_argument(
        "--media-file",
        type=Path,
        default=None,
        help="--via-oss 时使用已有本地媒体文件；不填则先用 yt-dlp 下载音频",
    )
    parser.add_argument(
        "--metadata-file",
        type=Path,
        default=None,
        help="复用已有 metadata.json，跳过本次 yt-dlp 元数据请求",
    )
    parser.add_argument(
        "--oss-object-key",
        default=None,
        help="OSS 对象路径；默认 youtube-asr/<视频ID>.<扩展名>",
    )
    parser.add_argument(
        "--oss-url-expires",
        type=int,
        default=3600,
        help="OSS 签名 URL 有效期秒数；默认 3600",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.poll_interval <= 0 or args.timeout <= 0:
        parser.error("--poll-interval 和 --timeout 必须是正整数")
    if args.oss_url_expires <= 0:
        parser.error("--oss-url-expires 必须是正整数")
    if args.media_file is not None and not args.via_oss:
        parser.error("--media-file 必须与 --via-oss 一起使用")

    try:
        source_url = validate_video_url(args.source)
    except YouTubeError as exc:
        print(f"[youtube-video] ERROR: {exc}", file=sys.stderr)
        return 2

    output = (args.out_dir or _default_output(source_url)).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    note_path = output / "note.md"
    manifest_path = output / "manifest.json"
    metadata = _base_metadata(source_url)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "platform": "youtube",
        "scope": "single",
        "status": "running",
        "source_url": source_url,
        "source_id": metadata.get("source_id"),
        "captured_at": utc_now(),
        "transcript_source": "asr",
        "output_dir": str(output),
        "note_path": str(note_path),
    }
    write_json(output / "metadata.json", metadata, sanitize=True)

    try:
        if args.metadata_file is not None:
            metadata_path = args.metadata_file.expanduser().resolve()
            print(f"[youtube-video] reusing metadata: {metadata_path}", flush=True)
            metadata = read_json(metadata_path)
            metadata_source_id = str(metadata.get("source_id") or "").strip()
            source_id = video_id_from_url(source_url)
            if metadata_source_id and metadata_source_id != source_id:
                raise YouTubeError(
                    "--metadata-file 中的 source_id 与当前 YouTube 链接不一致。"
                )
        else:
            print("[youtube-video] reading video metadata with yt-dlp...", flush=True)
            metadata = get_video_metadata(
                source_url,
                args.yt_dlp,
                args.cookies_from_browser,
            )
        write_json(output / "metadata.json", metadata, sanitize=True)
        manifest.update(
            {
                "source_id": metadata.get("source_id"),
                "title": metadata.get("title"),
                "author": metadata.get("author"),
                "published_at": metadata.get("published_at"),
            }
        )
        write_json(manifest_path, manifest, sanitize=True)

        media_path: Path | None = None
        if args.via_oss:
            if args.media_file is not None:
                media_path = args.media_file.expanduser().resolve()
                print(f"[youtube-video] using local media: {media_path}", flush=True)
            else:
                print("[youtube-video] downloading audio locally for OSS...", flush=True)
                media_path = download_audio(
                    source_url,
                    output / "video",
                    explicit_yt_dlp=args.yt_dlp,
                    cookies_from_browser=args.cookies_from_browser,
                )
                print(f"[youtube-video] downloaded media: {media_path}", flush=True)
            delivery_message = "uploading media to private OSS and submitting ASR"
        else:
            delivery_message = "resolving a temporary media URL and submitting ASR"
        print(f"[youtube-video] {delivery_message}...", flush=True)
        result = transcribe_media(
            source_url,
            output / "video",
            explicit_yt_dlp=args.yt_dlp,
            cookies_from_browser=args.cookies_from_browser,
            model=args.model,
            api_base_url=(
                args.api_base_url
                or os.environ.get("DASHSCOPE_API_BASE_URL")
                or DEFAULT_API_BASE_URL
            ),
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            language_hints=_parse_language_hints(args.language_hints),
            diarization=args.diarization,
            media_path=media_path,
            oss_object_key=args.oss_object_key,
            oss_url_expires=args.oss_url_expires,
        )
        write_video_note(
            note_path,
            metadata,
            scope="single",
            status="captured",
            transcript_source="asr",
            media_delivery=result.get("delivery"),
            transcript=result["transcription"],
            asr_model=result["model"],
            task_id=result["task_id"],
            transcript_path="video/transcript.md",
        )
        manifest.update(
            {
                "status": "captured",
                "asr": {
                    "status": "completed",
                    "model": result["model"],
                    "task_id": result["task_id"],
                    "delivery": result.get("delivery"),
                    "resolved_host": result["resolved_host"],
                    "oss_object_key": result.get("oss_object_key"),
                    "transcript_path": "video/transcript.md",
                },
                "completed_at": utc_now(),
            }
        )
        write_json(manifest_path, manifest, sanitize=True)
        print(f"[youtube-video] Markdown: {note_path}")
        print(f"[youtube-video] evidence: {output}")
        print("YOUTUBE_STATUS=captured")
        return 0
    except KeyboardInterrupt:
        error = "用户中断了 YouTube 转写。"
        status = "cancelled"
    except (OSError, YouTubeError) as exc:
        error = str(exc)
        status = "failed"

    write_video_note(
        note_path,
        metadata,
        scope="single",
        status=status,
        transcript_source="unavailable",
        error=error,
    )
    manifest.update(
        {
            "status": status,
            "transcript_source": "unavailable",
            "error": error[:3000],
            "updated_at": utc_now(),
        }
    )
    write_json(manifest_path, manifest, sanitize=True)
    print(f"[youtube-video] ERROR: {error}", file=sys.stderr)
    print(f"[youtube-video] Markdown (partial): {note_path}", file=sys.stderr)
    print(f"YOUTUBE_STATUS={status}")
    return 130 if status == "cancelled" else 2


if __name__ == "__main__":
    raise SystemExit(main())
