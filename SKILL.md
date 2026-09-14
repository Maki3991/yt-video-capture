---
name: yt-video-capture
description: Convert a specified YouTube video, or the first N public videos from a specified YouTube channel, into traceable local Markdown transcripts using yt-dlp and Bailian asynchronous ASR; safely preview and clean expired temporary OSS media. Do not use it for platform publishing, unrestricted discovery, or bypassing access controls.
---

# YouTube Video Capture

This Skill has three entry points:

- one YouTube video URL → one Markdown transcript;
- one public YouTube channel URL + N → the first N entries from the selected public channel tab, each archived as Markdown.
- “清空 OSS 内存” → preview and, only after confirmation, delete expired objects under `youtube-asr/`.

The default media route is:

```text
YouTube page URL
  → yt-dlp resolves a temporary audio/video URL
  → Bailian qwen-audio-3.0-asr-flash-filetrans asynchronous task
  → timestamped transcript + Markdown note + JSON evidence
```

When a YouTube CDN URL cannot be fetched by Bailian, the explicit fallback route is:

```text
YouTube page URL
  → yt-dlp local audio file
  → private OSS object + short-lived signed GET URL
  → Bailian qwen-audio-3.0-asr-flash-filetrans asynchronous task
  → timestamped transcript + Markdown note + JSON evidence
```

It deliberately does not reuse the XHS browser extension. YouTube public video and channel pages are handled by yt-dlp; the XHS Skill keeps its existing logged-in Chrome capture path.

## Workflow

1. One-time setup: ensure Python 3, `yt-dlp`, and a Bailian API key are available. Put the key only in the current process environment as `DASHSCOPE_API_KEY`. For the OSS route, install `oss2` and fill the ignored Skill-local `.env` with the RAM credentials and private Bucket settings. Do not write secrets into Markdown or JSON artifacts. The default route does not read browser cookies.
2. For one video, run:

   ```powershell
   python "<skill-root>\scripts\youtube_video_to_md.py" `
     "<youtube-video-url>" `
     --out-dir ".\youtube-video-results\one-video"
   ```

   If Bailian cannot fetch the YouTube CDN URL, use the explicit OSS route. With an existing local audio file:

   ```powershell
   python "<skill-root>\scripts\youtube_video_to_md.py" `
     "<youtube-video-url>" `
     --via-oss `
     --media-file ".\test-results\<run>\video\source.m4a" `
     --out-dir ".\youtube-video-results\one-video-oss"
   ```

3. Inspect the generated `note.md`, `manifest.json`, `metadata.json`, and `video/`. A completed note must have `YOUTUBE_STATUS=captured`, a non-empty transcript, and a successful `manifest.json`.
4. Only after the single-video route is manually checked, process a small channel pilot first:

   ```powershell
   python "<skill-root>\scripts\youtube_channel_to_md.py" `
     "<youtube-channel-url>" `
     --limit 5 `
     --run-name "channel-pilot"
   ```

   If YouTube returns a bot/login challenge, explicitly opt in to browser-cookie access for that run, for example `--cookies-from-browser chrome` or `--cookies-from-browser "chrome:Default"`. This is a sensitive choice and is never enabled implicitly.

5. For the requested first N videos, replace `--limit 5` with the user-specified N. The default tab is `/videos`; use `--channel-tab shorts` or `--channel-tab streams` only when explicitly requested. The batch is sequential by default, records each item's status, and can resume with `--run-dir <existing-run-dir>`.
6. Treat `notes/` as the user-facing output. Treat `run.json`, `source/`, and `items/` as provenance and recovery evidence.

## 清空 OSS 内存（临时文件清理）

这里的“清空 OSS 内存”不是删除整个 Bucket，而是清理本 Skill 上传的临时媒体文件。脚本读取 OSS 返回的每个对象的 `last_modified` 时间，计算：

```text
UTC 当前时间 - 保留天数 = 截止时间
对象 last_modified 早于截止时间 → 候选对象
```

脚本不是一直运行的后台程序；每次调用时扫描一次。若希望每天自动检查，可以之后用 Windows 任务计划程序每天触发同一个命令。任务计划程序只是负责“按时启动脚本”，时间判断仍由脚本完成。

先预览，默认绝不删除：

```powershell
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1 `
  --report ".\cleanup-results\preview.json"
```

预览会显示截止时间、候选对象、文件大小和最后修改时间。确认列表无误，并且确实要删除后，才允许执行：

```powershell
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1 `
  --execute `
  --confirm "DELETE youtube-asr/" `
  --report ".\cleanup-results\delete.json"
```

删除命令必须同时拥有 `--execute` 和精确的 `--confirm`，且只允许操作 `youtube-asr/` 及其子路径。交互式使用时，Skill 应先展示预览并获得用户对本次候选列表的明确确认，再运行删除命令。不要把前缀改成空值、`*` 或整个 Bucket 的根路径。

## Output contract

Each note keeps the common project fields:

```yaml
platform: youtube
source_url: original YouTube page URL
source_id: YouTube video ID
author: channel name
title: video title
published_at: original time when available
captured_at: local capture time
scope: single | creator_recent_n
transcript_source: asr | unavailable
media_delivery: youtube-cdn | oss-signed-url | unknown
status: captured | failed | cancelled
```

For a single video, the output directory contains:

```text
note.md
manifest.json
metadata.json
video/
  request-info.json
  resolution.json
  oss.json
  submit.json
  task.json
  transcription.json
  transcript.md
```

If a stage fails, `video/error.json` and a partial Markdown note are retained. A partial or failed note must not be described as a complete transcript.

For a channel batch:

```text
run.json
source/
  channel.json
  videos.json
items/
  001-<video-id>/metadata.json
  001-<video-id>/video/...
notes/
  001-<title>-<video-id>.md
```

The batch's “first N” means the order returned by yt-dlp for the public channel tab in that run. It is not a claim about private, members-only, deleted, hidden, or unavailable videos.

## Boundaries and evidence

- `yt-dlp` resolves a temporary signed URL; the Skill does not treat that URL as a permanent source and redacts query values in persisted diagnostics.
- The default route does not download the full video to local disk and does not require FFmpeg or OSS. If Bailian cannot fetch the resolved YouTube CDN URL, the Skill records failure; use `--via-oss` for the explicit local-upload fallback.
- The OSS fallback keeps the Bucket private, uploads only the selected local media file, and gives Bailian a short-lived signed GET URL. The signed URL is never persisted.
- `qwen-audio-3.0-asr-flash-filetrans` accepts one publicly reachable media URL per task and supports long media within the service's documented limits. The result URL is temporary and is downloaded immediately into `video/transcription.json`.
- By default the Skill does not read browser cookies. If the user explicitly supplies `--cookies-from-browser`, yt-dlp reads that browser profile for the current run only; the Skill never saves the cookies. This may expose the account to YouTube rate limits or account risk, so use it only when necessary. The Skill still does not bypass login, CAPTCHA, age gates, or access controls. Private, members-only, region-blocked, age-restricted, live, or deleted videos may fail.
- A batch completing means the pipeline finished its per-item work. It does not mean the transcript is factually correct; manually sample notes before using them as research material.
- Channel batches are intentionally sequential. This limits accidental API bursts and makes partial recovery understandable. Do not start a second batch for the same channel while one is running.
- Never persist `DASHSCOPE_API_KEY`, raw signed CDN URLs, or raw signed transcription-result URLs.
- OSS 清理默认是 dry-run；只有显式 `--execute` 加精确确认字符串才会删除对象。清理报告只保存对象键、大小和时间，不保存密钥或签名 URL。

## Individual debugging commands

```powershell
# check dependencies
yt-dlp --version
python "<skill-root>\scripts\youtube_video_to_md.py" --help
python "<skill-root>\scripts\youtube_channel_to_md.py" --help

# only when YouTube asks for a logged-in/browser session
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "<youtube-video-url>" `
  --cookies-from-browser chrome

# continue an interrupted batch
python "<skill-root>\scripts\youtube_channel_to_md.py" `
  "<youtube-channel-url>" `
  --run-dir ".\youtube-channel-results\<existing-run>"

# preview temporary OSS objects older than one day
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1
```
