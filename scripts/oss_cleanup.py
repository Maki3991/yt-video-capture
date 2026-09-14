#!/usr/bin/env python3
"""Preview and explicitly delete expired YouTube ASR objects from private OSS.

The script is intentionally not a daemon.  Each invocation lists the objects
under the protected ``youtube-asr/`` prefix, compares OSS' last-modified time
with a UTC cutoff, and optionally deletes the matching objects.

Safe default:
    python scripts/oss_cleanup.py --prefix youtube-asr/ --older-than-days 1

Deletion requires both ``--execute`` and an exact confirmation string.  This
keeps a scheduled invocation possible while making accidental deletion harder.
The Skill itself must still obtain action-time confirmation before invoking a
deleting command.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from youtube_asr import YouTubeError, _oss_settings, load_local_env, utc_now


DEFAULT_PREFIX = "youtube-asr/"
DEFAULT_OLDER_THAN_DAYS = 1.0
CONFIRMATION_PREFIX = "DELETE "


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "扫描私有 OSS 中超过保留时间的 YouTube ASR 临时文件；"
            "默认只预览，不删除。"
        )
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"只允许清理此保护前缀及其子路径；默认 {DEFAULT_PREFIX}",
    )
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=DEFAULT_OLDER_THAN_DAYS,
        help="对象最后修改时间早于多少天；默认 1",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="执行删除；不加此参数时永远只预览",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help='删除保护：必须精确填写，例如 "DELETE youtube-asr/"',
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="可选：把本次扫描和删除结果写入本地 JSON 报告",
    )
    return parser.parse_args(argv)


def _normalize_prefix(value: str) -> str:
    prefix = str(value or "").strip().lstrip("/")
    if prefix == DEFAULT_PREFIX.rstrip("/"):
        prefix = DEFAULT_PREFIX
    if not prefix or prefix in {"*", "."}:
        raise YouTubeError(
            f"--prefix 不能为空或通配整个 Bucket；只能使用 {DEFAULT_PREFIX} 及其子路径"
        )
    if "\\" in prefix or any(part == ".." for part in prefix.split("/")):
        raise YouTubeError("--prefix 不能包含反斜杠或 .. 路径段")
    if not prefix.startswith(DEFAULT_PREFIX):
        raise YouTubeError(
            f"出于安全原因，只允许清理 {DEFAULT_PREFIX} 及其子路径，"
            f"当前前缀为 {prefix}"
        )
    return prefix


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise YouTubeError(f"OSS 返回了无法解析的 last_modified：{value}") from exc
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise YouTubeError(f"OSS 返回了无法解析的 last_modified：{value}") from exc
    else:
        raise YouTubeError("OSS 对象缺少 last_modified，无法执行时间清理")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _make_bucket() -> tuple[Any, str]:
    try:
        import oss2
    except ImportError as exc:
        raise YouTubeError(
            "当前 Python 环境没有安装 oss2，请先执行：python -m pip install oss2"
        ) from exc

    access_key_id, access_key_secret, endpoint, bucket_name = _oss_settings()
    auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name), bucket_name


def _find_expired_objects(
    bucket: Any,
    prefix: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    try:
        import oss2
    except ImportError as exc:
        raise YouTubeError("当前 Python 环境没有安装 oss2") from exc

    expired: list[dict[str, Any]] = []
    for item in oss2.ObjectIterator(bucket, prefix=prefix):
        # ObjectIterator can also yield directory-like prefix entries.  They
        # have last_modified=None and must not be treated as files.
        if getattr(item, "is_prefix", lambda: False)():
            continue
        object_key = str(getattr(item, "key", "") or "").strip()
        if not object_key or not object_key.startswith(prefix):
            continue
        modified = _as_utc(getattr(item, "last_modified", None))
        if modified >= cutoff:
            continue
        expired.append(
            {
                "key": object_key,
                "size_bytes": int(getattr(item, "size", 0) or 0),
                "last_modified_utc": modified.isoformat(timespec="seconds"),
            }
        )
    return expired


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[oss-cleanup] report: {target}")


def _print_candidates(candidates: list[dict[str, Any]]) -> None:
    if not candidates:
        print("[oss-cleanup] candidates: 0")
        return
    total_bytes = sum(int(item["size_bytes"]) for item in candidates)
    print(
        f"[oss-cleanup] candidates: {len(candidates)} "
        f"({total_bytes} bytes)"
    )
    for item in candidates:
        print(
            f"  - {item['key']} | {item['size_bytes']} bytes | "
            f"last_modified={item['last_modified_utc']}"
        )


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    args = _parse_args(argv)
    if args.older_than_days <= 0:
        print("[oss-cleanup] ERROR: --older-than-days 必须是正数", file=sys.stderr)
        return 2

    try:
        prefix = _normalize_prefix(args.prefix)
        bucket, bucket_name = _make_bucket()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=args.older_than_days)
        candidates = _find_expired_objects(bucket, prefix, cutoff)
    except YouTubeError as exc:
        print(f"[oss-cleanup] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # OSS SDK errors are not all YouTubeError types.
        print(
            f"[oss-cleanup] ERROR: OSS 扫描失败：{str(exc)[:1500]}",
            file=sys.stderr,
        )
        return 1

    mode = "execute" if args.execute else "preview"
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": mode,
        "bucket": bucket_name,
        "prefix": prefix,
        "older_than_days": args.older_than_days,
        "cutoff_utc": cutoff.isoformat(timespec="seconds"),
        "scanned_at": utc_now(),
        "candidates": candidates,
        "deleted": [],
        "failed": [],
    }

    print(f"[oss-cleanup] mode: {mode}")
    print(f"[oss-cleanup] bucket: {bucket_name}")
    print(f"[oss-cleanup] prefix: {prefix}")
    print(f"[oss-cleanup] cutoff (UTC): {cutoff.isoformat(timespec='seconds')}")
    _print_candidates(candidates)

    if not args.execute:
        print("[oss-cleanup] preview only; no OSS object was deleted")
        try:
            _write_report(args.report, report)
        except OSError as exc:
            print(f"[oss-cleanup] ERROR: 无法写入报告：{exc}", file=sys.stderr)
            return 1
        return 0

    expected_confirmation = f"{CONFIRMATION_PREFIX}{prefix}"
    if args.confirm != expected_confirmation:
        print(
            "[oss-cleanup] ERROR: 删除未执行。请同时提供精确确认参数："
            f'--confirm "{expected_confirmation}"',
            file=sys.stderr,
        )
        return 2

    print("[oss-cleanup] deleting candidates...")
    for item in candidates:
        object_key = str(item["key"])
        try:
            result = bucket.delete_object(object_key)
            status = getattr(result, "status", None)
            if status not in (None, 204, 200):
                raise RuntimeError(f"HTTP status {status}")
            report["deleted"].append(item)
            print(f"[oss-cleanup] deleted: {object_key}")
        except Exception as exc:  # Keep going so one bad object is visible.
            failure = {**item, "error": str(exc)[:1500]}
            report["failed"].append(failure)
            print(f"[oss-cleanup] FAILED: {object_key}: {failure['error']}", file=sys.stderr)

    report["completed_at"] = utc_now()
    try:
        _write_report(args.report, report)
    except OSError as exc:
        print(f"[oss-cleanup] ERROR: 无法写入报告：{exc}", file=sys.stderr)
        return 1
    if report["failed"]:
        return 3
    print(f"[oss-cleanup] deleted count: {len(report['deleted'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
