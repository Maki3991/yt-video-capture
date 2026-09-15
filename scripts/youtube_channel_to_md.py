#!/usr/bin/env python3
"""Collect the first N public videos from a YouTube channel and transcribe them."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from youtube_asr import (
    DEFAULT_API_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    YouTubeError,
    build_transcript_contract,
    caption_result_for_persist,
    download_audio,
    download_caption,
    enumerate_channel,
    get_video_metadata,
    metadata_from_flat_item,
    safe_component,
    transcribe_media,
    utc_now,
    validate_channel_url,
    write_json,
    write_video_note,
)


def _parse_language_hints(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _new_run_dir(base: Path, run_name: str, channel_url: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    channel_hint = channel_url.rstrip("/").rsplit("/", 1)[-1]
    stem = safe_component(run_name or channel_hint or "youtube-channel", limit=60)
    return (base / f"{stem}-{stamp}").expanduser().resolve()


def _load_or_collect(
    run_root: Path,
    channel_url: str,
    limit: int,
    tab: str,
    explicit_yt_dlp: str | None,
    cookies_from_browser: str | None,
    cookies_file: str | Path | None,
) -> dict[str, Any]:
    source_dir = run_root / "source"
    channel_path = source_dir / "channel.json"
    videos_path = source_dir / "videos.json"
    if channel_path.is_file() and videos_path.is_file():
        channel_record = json_read(channel_path)
        videos = json_read(videos_path)
        if isinstance(videos, list) and channel_record.get("source_url") == channel_url:
            channel_record["videos"] = videos
            return channel_record

    record = enumerate_channel(
        channel_url,
        limit,
        tab=tab,
        explicit_yt_dlp=explicit_yt_dlp,
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )
    write_json(channel_path, {key: value for key, value in record.items() if key != "videos"}, sanitize=True)
    write_json(videos_path, record.get("videos", []), sanitize=True)
    return record


def json_read(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 YouTube 频道前 N 个公开视频转写为 Markdown。"
    )
    parser.add_argument("channel_url", help="YouTube 频道主页或频道 /videos 链接")
    parser.add_argument("--limit", type=int, default=50, help="采集数量；默认 50")
    parser.add_argument(
        "--channel-tab",
        choices=("videos", "shorts", "streams"),
        default="videos",
        help="采集频道标签；默认 videos",
    )
    parser.add_argument(
        "--run-name",
        default="",
        help="批次名称；默认从频道 URL 生成",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("youtube-channel-results"),
        help="批次根目录；默认 ./youtube-channel-results",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="指定已有批次目录时继续该批次；目录内需有 run.json/source/",
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
        "--cookies",
        dest="cookies_file",
        type=Path,
        default=None,
        metavar="PATH",
        help="使用 Mozilla/Netscape 格式的 Cookie 文件；不要放入仓库",
    )
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
        help="每个视频最长等待秒数；默认 7200",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="视频之间的等待秒数；默认 1，避免连续请求过密",
    )
    parser.add_argument(
        "--language-hints",
        default=None,
        help="可选语言提示，逗号分隔，例如 zh,en",
    )
    parser.add_argument(
        "--caption-languages",
        default=None,
        help="字幕语言优先顺序，逗号分隔，例如 zh-Hans,en；不填则按 yt-dlp 返回顺序",
    )
    parser.add_argument(
        "--allow-translated-captions",
        action="store_true",
        help="允许使用 YouTube 自动翻译字幕；默认只用人工或原始自动字幕",
    )
    parser.add_argument(
        "--diarization",
        action="store_true",
        help="请求说话人分离；长视频建议谨慎使用",
    )
    return parser


def _initial_manifest(run_root: Path, source: dict[str, Any]) -> dict[str, Any]:
    videos = source.get("videos")
    videos = videos if isinstance(videos, list) else []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(videos, start=1):
        if not isinstance(item, dict):
            item = {}
        source_url = str(item.get("source_url") or "").strip()
        item_contract = build_transcript_contract(
            metadata_from_flat_item(item, source_url),
            scope="creator_recent_n",
            status="pending",
            transcript_source=None,
            asr_status="pending",
        )
        items.append({"index": index, **item_contract})
    return {
        "schema_version": 1,
        "platform": "youtube",
        "scope": "creator_recent_n",
        "status": "running",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "run_dir": str(run_root),
        "source_url": source.get("source_url"),
        "tab_url": source.get("tab_url"),
        "channel_tab": source.get("tab"),
        "requested_count": source.get("requested_count"),
        "collected_count": source.get("collected_count"),
        "sufficient": source.get("sufficient"),
        "collection_order": source.get("collection_order"),
        "warning": source.get("warning") or None,
        "items": items,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubeError(f"无法读取批次 manifest：{path}\n{exc}") from exc
    if not isinstance(value, dict):
        raise YouTubeError("批次 manifest 顶层不是对象。")
    return value


def _prepare_run(args: argparse.Namespace, channel_url: str) -> tuple[Path, dict[str, Any]]:
    if args.run_dir is not None:
        run_root = args.run_dir.expanduser().resolve()
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        run_root = _new_run_dir(args.out_dir, args.run_name, channel_url)
        run_root.mkdir(parents=True, exist_ok=False)

    run_manifest_path = run_root / "run.json"
    if run_manifest_path.is_file():
        run_manifest = _read_manifest(run_manifest_path)
        if str(run_manifest.get("source_url") or "") != channel_url:
            raise YouTubeError("已有批次目录的 source_url 与本次频道链接不一致。")
        return run_root, run_manifest

    source = _load_or_collect(
        run_root,
        channel_url,
        args.limit,
        args.channel_tab,
        args.yt_dlp,
        args.cookies_from_browser,
        args.cookies_file,
    )
    run_manifest = _initial_manifest(run_root, source)
    write_json(run_manifest_path, run_manifest, sanitize=True)
    return run_root, run_manifest


def _item_dir(run_root: Path, index: int, item: dict[str, Any]) -> Path:
    source_id = safe_component(item.get("source_id") or f"item-{index}", limit=32)
    return run_root / "items" / f"{index:03d}-{source_id}"


def _note_path(run_root: Path, index: int, metadata: dict[str, Any]) -> Path:
    title = safe_component(metadata.get("title") or "未命名视频", fallback="video", limit=90)
    source_id = safe_component(metadata.get("source_id") or f"item-{index}", limit=24)
    return run_root / "notes" / f"{index:03d}-{title}-{source_id}.md"


def _update_item(run_manifest: dict[str, Any], index: int, updates: dict[str, Any]) -> None:
    items = run_manifest.get("items")
    if not isinstance(items, list) or index < 1 or index > len(items):
        return
    item = items[index - 1]
    if isinstance(item, dict):
        item.update(updates)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cookies_from_browser and args.cookies_file:
        parser.error("--cookies-from-browser 与 --cookies 只能二选一")
    if args.limit <= 0 or args.poll_interval <= 0 or args.timeout <= 0 or args.delay < 0:
        parser.error("--limit、--poll-interval、--timeout 必须为正数，--delay 不能为负数")

    try:
        channel_url = validate_channel_url(args.channel_url)
        run_root, run_manifest = _prepare_run(args, channel_url)
    except (OSError, YouTubeError) as exc:
        print(f"[youtube-channel] ERROR: {exc}", file=sys.stderr)
        return 2

    run_manifest_path = run_root / "run.json"
    run_manifest["status"] = "running"
    run_manifest["updated_at"] = utc_now()
    write_json(run_manifest_path, run_manifest, sanitize=True)
    items = run_manifest.get("items")
    items = items if isinstance(items, list) else []
    language_hints = _parse_language_hints(args.language_hints)
    api_base_url = (
        args.api_base_url
        or os.environ.get("DASHSCOPE_API_BASE_URL")
        or DEFAULT_API_BASE_URL
    )
    failed = False

    if not items:
        run_manifest["status"] = "failed"
        run_manifest["error"] = "频道列表没有发现可处理的视频。"
        run_manifest["updated_at"] = utc_now()
        write_json(run_manifest_path, run_manifest, sanitize=True)
        print("[youtube-channel] ERROR: 频道列表没有发现可处理的视频。", file=sys.stderr)
        print("BATCH_STATUS=failed")
        return 2

    print(f"[youtube-channel] 批次目录：{run_root}")
    print(f"[youtube-channel] 待处理视频：{len(items)}")
    try:
        for index, flat_item in enumerate(items, start=1):
            if not isinstance(flat_item, dict):
                continue
            existing_status = str(flat_item.get("status") or "")
            note_existing = str(flat_item.get("note_path") or "")
            if existing_status == "captured" and note_existing and Path(note_existing).is_file():
                print(f"[youtube-channel] {index}/{len(items)} 已完成，跳过")
                continue

            source_url = str(flat_item.get("source_url") or "").strip()
            if not source_url:
                error = "列表项没有 source_url。"
                _update_item(run_manifest, index, {"status": "failed", "error": error})
                failed = True
                write_json(run_manifest_path, run_manifest, sanitize=True)
                continue

            item_dir = _item_dir(run_root, index, flat_item)
            item_dir.mkdir(parents=True, exist_ok=True)
            metadata = metadata_from_flat_item(flat_item, source_url)
            metadata_error = ""
            try:
                print(f"[youtube-channel] {index}/{len(items)} 读取元数据：{source_url}", flush=True)
                metadata = get_video_metadata(
                    source_url,
                    args.yt_dlp,
                    args.cookies_from_browser,
                    args.cookies_file,
                )
            except YouTubeError as exc:
                metadata_error = str(exc)
                print(
                    f"[youtube-channel] WARNING: 元数据补充失败，将继续尝试转写：{metadata_error}",
                    file=sys.stderr,
                )
            write_json(item_dir / "metadata.json", metadata, sanitize=True)
            note_path = _note_path(run_root, index, metadata)
            relative_transcript = f"../items/{item_dir.name}/video/transcript.md"
            item_updates: dict[str, Any] = {
                **build_transcript_contract(
                    metadata,
                    scope="creator_recent_n",
                    status="running",
                    transcript_source=None,
                    asr_status="pending",
                    captured_at=str(flat_item.get("captured_at") or ""),
                ),
                "metadata_path": str(item_dir / "metadata.json"),
                "evidence_dir": str(item_dir),
                "note_path": str(note_path),
                "attempted_at": utc_now(),
            }
            if metadata_error:
                item_updates["metadata_error"] = metadata_error[:3000]
            caption_result = download_caption(
                source_url,
                item_dir / "video",
                language_preferences=_parse_language_hints(args.caption_languages),
                allow_translated=args.allow_translated_captions,
                explicit_yt_dlp=args.yt_dlp,
                cookies_from_browser=args.cookies_from_browser,
                cookies_file=args.cookies_file,
            )
            item_updates["captions"] = caption_result_for_persist(caption_result)
            _update_item(run_manifest, index, item_updates)
            write_json(run_manifest_path, run_manifest, sanitize=True)

            if caption_result.get("status") == "succeeded":
                caption_path = f"video/{caption_result['raw_path']}"
                write_video_note(
                    note_path,
                    metadata,
                    scope="creator_recent_n",
                    status="captured",
                    transcript_source="platform_caption",
                    caption_language=caption_result.get("caption_language"),
                    caption_type=caption_result.get("caption_type"),
                    asr_status="skipped",
                    transcript=caption_result["transcription"],
                    transcript_path=relative_transcript,
                    caption_path=caption_path,
                )
                _update_item(
                    run_manifest,
                    index,
                    {
                        **build_transcript_contract(
                            metadata,
                            scope="creator_recent_n",
                            status="captured",
                            transcript_source="platform_caption",
                            caption_language=caption_result.get("caption_language"),
                            caption_type=caption_result.get("caption_type"),
                            asr_status="skipped",
                            captured_at=str(flat_item.get("captured_at") or ""),
                        ),
                        "captions": caption_result_for_persist(caption_result),
                        "completed_at": utc_now(),
                    },
                )
                write_json(run_manifest_path, run_manifest, sanitize=True)
                print(
                    f"[youtube-channel] {index}/{len(items)} 使用 YouTube 字幕（"
                    f"{caption_result.get('caption_language')}/{caption_result.get('caption_type')}），"
                    "已跳过 ASR"
                )
                if args.delay and index < len(items):
                    time.sleep(args.delay)
                continue

            print(
                f"[youtube-channel] {index}/{len(items)} 字幕不可用（"
                f"{caption_result.get('reason') or 'unknown'}），回退到 ASR...",
                flush=True,
            )
            try:
                print(f"[youtube-channel] {index}/{len(items)} 下载本地音频并上传 OSS：{metadata.get('title')}", flush=True)
                media_path = download_audio(
                    source_url,
                    item_dir / "video",
                    explicit_yt_dlp=args.yt_dlp,
                    cookies_from_browser=args.cookies_from_browser,
                    cookies_file=args.cookies_file,
                )
                result = transcribe_media(
                    source_url,
                    item_dir / "video",
                    model=args.model,
                    api_base_url=api_base_url,
                    poll_interval=args.poll_interval,
                    timeout=args.timeout,
                    language_hints=language_hints,
                    diarization=args.diarization,
                    media_path=media_path,
                )
                write_video_note(
                    note_path,
                    metadata,
                    scope="creator_recent_n",
                    status="captured",
                    transcript_source="asr",
                    asr_status="completed",
                    media_delivery=result.get("delivery"),
                    transcript=result["transcription"],
                    asr_model=result["model"],
                    task_id=result["task_id"],
                    transcript_path=relative_transcript,
                )
                _update_item(
                    run_manifest,
                    index,
                    {
                        **build_transcript_contract(
                            metadata,
                            scope="creator_recent_n",
                            status="captured",
                            transcript_source="asr",
                            asr_status="completed",
                            asr_model=result["model"],
                            task_id=result["task_id"],
                            media_delivery=result.get("delivery"),
                        ),
                        "resolved_host": result["resolved_host"],
                        "completed_at": utc_now(),
                    },
                )
                print(f"[youtube-channel] {index}/{len(items)} 完成：{note_path}")
            except KeyboardInterrupt:
                raise
            except (OSError, YouTubeError) as exc:
                error = str(exc)
                if caption_result.get("status") in {"skipped", "failed"}:
                    caption_reason = caption_result.get("reason") or "caption_unavailable"
                    error = f"字幕阶段 {caption_reason}；随后 ASR 阶段：{error}"
                failed = True
                write_video_note(
                    note_path,
                    metadata,
                    scope="creator_recent_n",
                    status="failed",
                    transcript_source="unavailable",
                    asr_status="failed",
                    asr_model=args.model,
                    media_delivery="oss-signed-url",
                    transcript_path=relative_transcript,
                    error=error,
                )
                _update_item(
                    run_manifest,
                    index,
                    {
                        **build_transcript_contract(
                            metadata,
                            scope="creator_recent_n",
                            status="failed",
                            transcript_source="unavailable",
                            asr_status="failed",
                            asr_model=args.model,
                            media_delivery="oss-signed-url",
                        ),
                        "error": error[:3000],
                        "completed_at": utc_now(),
                    },
                )
                print(f"[youtube-channel] {index}/{len(items)} FAILED: {error}", file=sys.stderr)

            run_manifest["updated_at"] = utc_now()
            write_json(run_manifest_path, run_manifest, sanitize=True)
            if args.delay and index < len(items):
                time.sleep(args.delay)
    except KeyboardInterrupt:
        run_manifest["status"] = "cancelled"
        run_manifest["updated_at"] = utc_now()
        write_json(run_manifest_path, run_manifest, sanitize=True)
        print("[youtube-channel] 已取消；可用 --run-dir 从当前批次继续。", file=sys.stderr)
        print("BATCH_STATUS=cancelled")
        return 130

    run_manifest["status"] = "partial" if failed else "done"
    run_manifest["updated_at"] = utc_now()
    write_json(run_manifest_path, run_manifest, sanitize=True)
    print(f"[youtube-channel] 最终 Markdown：{run_root / 'notes'}")
    print(f"[youtube-channel] 证据目录：{run_root / 'items'}")
    print(f"BATCH_STATUS={run_manifest['status']}")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
