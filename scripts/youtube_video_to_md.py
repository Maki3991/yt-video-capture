#!/usr/bin/env python3
"""Convert one YouTube video into a traceable Markdown transcript."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from youtube_asr import (
    DEFAULT_API_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    YouTubeError,
    artifact_path_for_output,
    build_transcript_contract,
    caption_result_for_persist,
    clean_video_metadata,
    download_audio,
    get_video_metadata,
    initial_stage_states,
    import_browser_transcript,
    read_json,
    safe_component,
    safe_error,
    transcribe_media,
    utc_now,
    update_stage_state,
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


def _record_stage(
    manifest: dict[str, Any],
    manifest_path: Path,
    output_root: Path,
    source_url: str,
    stage: str,
    status: str,
    *,
    artifact_paths: list[str] | tuple[str, ...] = (),
    artifact_root: Path | None = None,
    error: str | None = None,
    retryable: bool | None = None,
    reason: str | None = None,
) -> None:
    root = artifact_root or output_root
    paths = [
        artifact_path_for_output(
            path,
            artifact_root=root,
            output_root=output_root,
        )
        for path in artifact_paths
        if str(path or "").strip()
    ]
    update_stage_state(
        manifest.setdefault("stages", initial_stage_states()),
        stage,
        status,
        artifact_paths=paths,
        error=safe_error(error, source_url) if error else None,
        retryable=retryable,
        reason=reason,
    )
    write_json(manifest_path, manifest, sanitize=True)


def _make_asr_stage_callback(
    manifest: dict[str, Any],
    manifest_path: Path,
    output_root: Path,
    source_url: str,
    artifact_root: Path,
) -> Callable[..., None]:
    def callback(
        stage: str,
        status: str,
        *,
        artifact_paths: list[str] | tuple[str, ...] = (),
        error: str | None = None,
        retryable: bool | None = None,
        reason: str | None = None,
    ) -> None:
        _record_stage(
            manifest,
            manifest_path,
            output_root,
            source_url,
            stage,
            status,
            artifact_paths=tuple(artifact_paths),
            artifact_root=artifact_root,
            error=error,
            retryable=retryable,
            reason=reason,
        )

    return callback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将一个 YouTube 视频的浏览器字幕或本地音频转为 Markdown。"
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
    parser.add_argument(
        "--cookies",
        dest="cookies_file",
        type=Path,
        default=None,
        metavar="PATH",
        help="使用 Mozilla/Netscape 格式的 Cookie 文件；不指定时自动检查固定 Cookie 目录；不要放入仓库",
    )
    parser.add_argument(
        "--browser-transcript-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Computer Use 从已登录 YouTube 页面导出的文字稿文件；提供后跳过 yt-dlp、OSS 和百炼",
    )
    parser.add_argument(
        "--browser-no-transcript",
        action="store_true",
        help="Computer Use 已检查页面但没有可用 Transcript；直接进入本地音频→OSS→百炼",
    )
    parser.add_argument(
        "--browser-title",
        default=None,
        help="浏览器页面标题；用于补充本地 note 的视频标题",
    )
    parser.add_argument(
        "--browser-author",
        default=None,
        help="浏览器页面可见的频道名；可选",
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
        "--media-file",
        type=Path,
        default=None,
        help="ASR 时使用已有本地媒体文件；不填则先用 yt-dlp 下载音频再上传 OSS",
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
    if args.cookies_from_browser and args.cookies_file:
        parser.error("--cookies-from-browser 与 --cookies 只能二选一")
    if args.browser_transcript_file is not None and args.browser_no_transcript:
        parser.error("--browser-transcript-file 与 --browser-no-transcript 只能二选一")
    if args.browser_transcript_file is not None and (
        args.cookies_from_browser
        or args.cookies_file
        or args.metadata_file is not None
        or args.media_file is not None
    ):
        parser.error(
            "--browser-transcript-file 不能与 Cookie、--metadata-file 或 --media-file 一起使用"
        )
    if args.browser_no_transcript and args.metadata_file is not None:
        parser.error("--browser-no-transcript 不能与 --metadata-file 一起使用")
    if (args.browser_title or args.browser_author) and not (
        args.browser_transcript_file or args.browser_no_transcript
    ):
        parser.error("--browser-title/--browser-author 必须与浏览器字幕或 --browser-no-transcript 一起使用")
    if args.poll_interval <= 0 or args.timeout <= 0:
        parser.error("--poll-interval 和 --timeout 必须是正整数")
    if args.oss_url_expires <= 0:
        parser.error("--oss-url-expires 必须是正整数")
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
        **build_transcript_contract(
            metadata,
            scope="single",
            status="running",
            transcript_source=None,
            asr_status="pending",
        ),
        "output_dir": str(output),
        "note_path": str(note_path),
        "stages": initial_stage_states(),
    }
    write_json(output / "metadata.json", metadata, sanitize=True)
    write_json(manifest_path, manifest, sanitize=True)
    caption_result: dict[str, Any] = {"status": "not_run"}
    active_stage: str | None = None

    try:
        if args.browser_transcript_file is not None:
            print("[youtube-video] importing Computer Use browser transcript...", flush=True)
            active_stage = "browser_transcript"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
                artifact_root=output / "video",
            )
            browser_result = import_browser_transcript(
                source_url,
                args.browser_transcript_file,
                output / "video",
                title=args.browser_title,
                author=args.browser_author,
            )
            metadata = browser_result.pop("metadata")
            caption_result = browser_result
            active_stage = "metadata"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
            )
        elif args.browser_no_transcript:
            print(
                "[youtube-video] Computer Use 已确认没有可用 Transcript；"
                "跳过 yt-dlp 字幕检查，进入本地音频→OSS→百炼...",
                flush=True,
            )
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                "browser_transcript",
                "skipped",
                reason="no_usable_transcript",
            )
            metadata = _base_metadata(source_url)
            if args.browser_title:
                metadata["title"] = " ".join(args.browser_title.split())
            if args.browser_author:
                metadata["author"] = " ".join(args.browser_author.split())
            metadata["metadata_source"] = "computer_use"
            active_stage = "metadata"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
            )
        elif args.metadata_file is not None:
            metadata_path = args.metadata_file.expanduser().resolve()
            print(f"[youtube-video] reusing metadata: {metadata_path}", flush=True)
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                "browser_transcript",
                "skipped",
                reason="browser_check_not_supplied",
            )
            active_stage = "metadata"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
            )
            metadata = read_json(metadata_path)
            metadata_source_id = str(metadata.get("source_id") or "").strip()
            source_id = video_id_from_url(source_url)
            if metadata_source_id and metadata_source_id != source_id:
                raise YouTubeError(
                    "--metadata-file 中的 source_id 与当前 YouTube 链接不一致。"
                )
        else:
            print("[youtube-video] reading video metadata with yt-dlp...", flush=True)
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                "browser_transcript",
                "skipped",
                reason="browser_check_not_supplied",
            )
            active_stage = "metadata"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
            )
            metadata = get_video_metadata(
                source_url,
                args.yt_dlp,
                args.cookies_from_browser,
                args.cookies_file,
            )
        write_json(output / "metadata.json", metadata, sanitize=True)
        _record_stage(
            manifest,
            manifest_path,
            output,
            source_url,
            "metadata",
            "succeeded",
            artifact_paths=("metadata.json",),
        )
        if args.browser_transcript_file is not None:
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                "browser_transcript",
                "succeeded",
                artifact_paths=(
                    f"video/{caption_result['raw_path']}",
                    f"video/{caption_result['transcript_path']}",
                    f"video/{caption_result['selection_path']}",
                ),
                artifact_root=output,
            )
        active_stage = None
        manifest.update(
            {
                "source_id": metadata.get("source_id"),
                "title": metadata.get("title"),
                "author": metadata.get("author"),
                "published_at": metadata.get("published_at"),
            }
        )
        write_json(manifest_path, manifest, sanitize=True)

        if args.browser_transcript_file is not None:
            manifest["captions"] = caption_result_for_persist(caption_result)
        elif args.browser_no_transcript:
            manifest["captions"] = {
                "status": "not_available",
                "retrieval_method": "computer_use",
                "reason": "no_usable_transcript",
            }
        else:
            manifest["captions"] = {
                "status": "not_run",
                "retrieval_method": "computer_use",
                "reason": "browser_check_not_supplied",
            }
        write_json(manifest_path, manifest, sanitize=True)
        if caption_result.get("status") == "succeeded":
            caption_path = f"video/{caption_result['raw_path']}"
            active_stage = "markdown_render"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
                artifact_root=output,
            )
            write_video_note(
                note_path,
                metadata,
                scope="single",
                status="captured",
                transcript_source="platform_caption",
                caption_language=caption_result.get("caption_language"),
                caption_type=caption_result.get("caption_type"),
                asr_status="skipped",
                transcript=caption_result["transcription"],
                transcript_path="video/transcript.md",
                caption_path=caption_path,
            )
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "succeeded",
                artifact_paths=("video/transcript.md", "note.md"),
                artifact_root=output,
            )
            active_stage = None
            manifest.update(
                {
                    **build_transcript_contract(
                        metadata,
                        scope="single",
                        status="captured",
                        transcript_source="platform_caption",
                        caption_language=caption_result.get("caption_language"),
                        caption_type=caption_result.get("caption_type"),
                        asr_status="skipped",
                        captured_at=str(manifest.get("captured_at") or ""),
                    ),
                    "captions": caption_result_for_persist(caption_result),
                    "completed_at": utc_now(),
                }
            )
            write_json(manifest_path, manifest, sanitize=True)
            print(
                f"[youtube-video] 使用 YouTube 字幕（{caption_result.get('caption_language')}/"
                f"{caption_result.get('caption_type')}），已跳过 ASR"
            )
            print(f"[youtube-video] Markdown: {note_path}")
            print(f"[youtube-video] evidence: {output}")
            print("YOUTUBE_STATUS=captured")
            return 0
        if args.browser_transcript_file is None:
            print(
                "[youtube-video] 未使用浏览器字幕；进入本地音频→私有 OSS→百炼 ASR...",
                flush=True,
            )

        media_path: Path
        if args.media_file is not None:
            media_path = args.media_file.expanduser().resolve()
            if not media_path.is_file():
                _record_stage(
                    manifest,
                    manifest_path,
                    output,
                    source_url,
                    "media_download",
                    "failed",
                    error=f"找不到待上传的本地媒体文件：{media_path}",
                    retryable=False,
                )
                raise YouTubeError(f"找不到待上传的本地媒体文件：{media_path}")
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                "media_download",
                "skipped",
                artifact_paths=(str(media_path),),
                reason="local_media_supplied",
            )
            print(f"[youtube-video] using local media: {media_path}", flush=True)
        else:
            print("[youtube-video] downloading audio locally for OSS...", flush=True)
            active_stage = "media_download"
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "running",
            )
            media_path = download_audio(
                source_url,
                output / "video",
                explicit_yt_dlp=args.yt_dlp,
                cookies_from_browser=args.cookies_from_browser,
                cookies_file=args.cookies_file,
            )
            _record_stage(
                manifest,
                manifest_path,
                output,
                source_url,
                active_stage,
                "succeeded",
                artifact_paths=(str(media_path),),
                artifact_root=output,
            )
            active_stage = None
            print(f"[youtube-video] downloaded media: {media_path}", flush=True)
        print("[youtube-video] uploading media to private OSS and submitting ASR...", flush=True)
        result = transcribe_media(
            source_url,
            output / "video",
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
            on_stage=_make_asr_stage_callback(
                manifest,
                manifest_path,
                output,
                source_url,
                output / "video",
            ),
        )
        active_stage = "markdown_render"
        write_video_note(
            note_path,
            metadata,
            scope="single",
            status="captured",
            transcript_source="asr",
            asr_status="completed",
            media_delivery=result.get("delivery"),
            transcript=result["transcription"],
            asr_model=result["model"],
            task_id=result["task_id"],
            transcript_path="video/transcript.md",
        )
        _record_stage(
            manifest,
            manifest_path,
            output,
            source_url,
            active_stage,
            "succeeded",
            artifact_paths=("note.md",),
            artifact_root=output,
        )
        active_stage = None
        manifest.update(
            {
                **build_transcript_contract(
                    metadata,
                    scope="single",
                    status="captured",
                    transcript_source="asr",
                    asr_status="completed",
                    asr_model=result["model"],
                    task_id=result["task_id"],
                    media_delivery=result.get("delivery"),
                    captured_at=str(manifest.get("captured_at") or ""),
                ),
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
        error = safe_error(str(exc), source_url)
        status = "failed"

    if active_stage:
        _record_stage(
            manifest,
            manifest_path,
            output,
            source_url,
            active_stage,
            "cancelled" if status == "cancelled" else "failed",
            error=error,
            retryable=status == "cancelled" or active_stage != "asr_submit",
        )

    error_path = output / "video" / "error.json"
    if not error_path.is_file():
        write_json(
            error_path,
            {
                "status": status,
                "error": error,
                "failed_stage": active_stage,
                "recorded_at": utc_now(),
            },
        )

    if caption_result.get("status") in {"skipped", "failed"}:
        caption_reason = caption_result.get("reason") or "caption_unavailable"
        error = f"字幕阶段 {caption_reason}；随后 ASR 阶段：{error}"

    write_video_note(
        note_path,
        metadata,
        scope="single",
        status=status,
        transcript_source="unavailable",
        asr_status="cancelled" if status == "cancelled" else "failed",
        asr_model=args.model,
        media_delivery=None if args.browser_transcript_file is not None else "oss-signed-url",
        error=error,
    )
    manifest.update(
        {
            **build_transcript_contract(
                metadata,
                scope="single",
                status=status,
                transcript_source="unavailable",
                asr_status="cancelled" if status == "cancelled" else "failed",
                asr_model=args.model,
                media_delivery=None if args.browser_transcript_file is not None else "oss-signed-url",
                captured_at=str(manifest.get("captured_at") or ""),
            ),
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
