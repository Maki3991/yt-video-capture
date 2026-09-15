---
name: yt-video-capture
description: Capture a specified YouTube video's transcript through an authorized Computer Use browser session, or when no usable browser transcript exists download local audio with yt-dlp, upload it to private OSS, and use Bailian ASR; also process the first N public channel videos and safely clean temporary OSS media. Do not use it for platform publishing, unrestricted discovery, or bypassing access controls.
---

# YouTube Video Capture

This Skill has three entry points:

- one YouTube video URL → one Markdown transcript, using the browser-caption or local-audio→OSS→Bailian route;
- one public YouTube channel URL + N → the first N entries from the selected public channel tab, each archived as Markdown.
- “清空 OSS 内存” → preview and, only after confirmation, delete expired objects under `youtube-asr/`.

For a single video, the Skill-level route is:

```text
YouTube page URL
  → Computer Use opens the user's authorized Chrome YouTube page and skips an ad when a skip button appears
  → usable YouTube Transcript: export it and import locally
  → no usable Transcript: yt-dlp downloads local audio
  → private OSS object + short-lived signed GET URL
  → Bailian qwen-audio-3.0-asr-flash-filetrans asynchronous task
  → timestamped transcript + Markdown note + JSON evidence
```

The Python single-video script does not read YouTube captions with yt-dlp and does not send a YouTube CDN URL to Bailian. It receives either the browser-exported transcript or the explicit `--browser-no-transcript` signal from the Skill layer. The latter starts the local-audio→OSS→Bailian route.

The browser route is read-only: it uses the user's authorized Chrome page only to obtain the visible YouTube Transcript and click a visible ad-skip control. It does not read or export Cookie, submit data to YouTube, or bypass login, CAPTCHA, age gates, or access controls. Channel batches remain yt-dlp-based for now; their ASR fallback also uses local audio→OSS→Bailian.

## Computer Use 广告门槛（必须完成）

YouTube 的广告可能在几秒后出现“跳过广告”，也可能没有跳过按钮、连续播放多条广告，或直到倒计时结束才进入主视频。对每个单视频浏览器任务，必须先完成下面的门槛；在门槛通过前，禁止打开或导出 Transcript：

1. 导航到目标 `/watch` 页面后，先等待至少 10 秒；每 2～3 秒观察一次播放器状态，最长等待 60 秒。10 秒只是最短观察时间，不代表广告已经结束。
2. 如果出现可见的“跳过广告”按钮，立即点击；等待约 3 秒后再次观察。如果又出现第二条广告，重复点击和观察。
3. 如果始终没有跳过按钮，继续等待广告自然结束。没有按钮不是可以提前导出 Transcript 的理由。
4. 只有同时确认广告遮罩、广告倒计时和“跳过广告”控件都消失，页面仍是用户给定的视频，且主视频画面/播放器已经出现（正在播放时确认时间轴在推进）后，才算广告清除；清除后再等待约 3 秒。
5. 现在才调用 Transcript 导出。检查导出文本开头：如果明显是广告、赞助商或与目标视频无关的内容，丢弃这次导出，重新执行本门槛，最多重试 2 次。60 秒后仍无法确认主视频开始时，记录 `ad_not_cleared` 并停止，不得把广告字幕保存为成功文字稿。

## Workflow

1. One-time setup: ensure Python 3, `yt-dlp`, Node.js 22+, and the official `yt-dlp-ejs` package are available. The shared yt-dlp helper automatically adds `--js-runtimes node` when Node.js is found on PATH. It does not read the Chrome profile directly; when present, it automatically uses the newest Netscape/Mozilla Cookie file in `D:\Softwares\Programming Projects\_yt-cookies\` (prefer the stable name `youtube-cookies.txt`). A video with a usable browser Transcript does not need `DASHSCOPE_API_KEY`, `oss2`, or OSS credentials. The no-Transcript route needs `DASHSCOPE_API_KEY`, `oss2`, and the ignored Skill-local `.env` with the RAM credentials and private Bucket settings. Do not write secrets into Markdown or JSON artifacts.
2. For one video, use the authorized Computer Use browser route as the first branch:

   - select the user's already-open YouTube Chrome tab;
   - navigate to the user-provided /watch URL if needed;
   - complete the mandatory **Computer Use 广告门槛** above before any Transcript action;
   - only after the gate passes, call the browser tab's transcript export capability;
   - if export succeeds, capture the returned UTF-8 text file path and visible page title, then run the local importer:

   ~~~powershell
   python "<skill-root>\scripts\youtube_video_to_md.py" "<youtube-video-url>" --browser-transcript-file "<computer-use-exported-txt>" --browser-title "<visible-video-title>" --out-dir ".\youtube-video-results\browser-one-video"
   ~~~

   The importer validates the exported video ID, retains the browser export under video/captions/, and never calls yt-dlp, OSS, or Bailian.

3. If Computer Use confirms that the page has no usable Transcript, pass the browser result to the local script and go straight to OSS ASR:

   ```powershell
   python "<skill-root>\scripts\youtube_video_to_md.py" `
     "<youtube-video-url>" `
     --browser-no-transcript `
     --browser-title "<visible-video-title>" `
     --browser-author "<visible-channel-name>" `
     --out-dir ".\youtube-video-results\one-video-asr"
   ```

   The script uses `yt-dlp` only to download a local audio file, uploads it to the private OSS Bucket, and gives Bailian only the short-lived OSS signed URL. If a local audio file already exists, add `--media-file` to skip the download. There is no YouTube CDN direct-link fallback.

4. Inspect the generated `note.md`, `manifest.json`, `metadata.json`, and `video/`. A completed note must have `YOUTUBE_STATUS=captured`, a non-empty transcript, and a successful `manifest.json`. Browser-caption output must record `retrieval_method=computer_use` and `asr_status=skipped`; ASR output must record `media_delivery=oss-signed-url` and an `oss.json` artifact.
   `manifest.json` also records a `stages` object. Each stage is written back immediately and contains `status`, `started_at`, `completed_at`, `attempt`, `retryable`, `error`, `reason`, and `artifact_paths`; a failed or cancelled stage must remain visible even when a later fallback succeeds.
5. Only after the single-video route is manually checked, process a small channel pilot first:

   ```powershell
   python "<skill-root>\scripts\youtube_channel_to_md.py" `
     "<youtube-channel-url>" `
     --limit 5 `
     --run-name "channel-pilot"
   ```

   If YouTube returns a bot/login challenge and the fixed Cookie directory is empty or its file has expired, refresh the exported Cookie file there. As a one-run alternative, explicitly opt in to browser-cookie access with `--cookies-from-browser chrome` or pass `--cookies <path>`. Browser-profile access is never enabled implicitly.

6. For the requested first N videos, replace `--limit 5` with the user-specified N. The default tab is `/videos`; use `--channel-tab shorts` or `--channel-tab streams` only when explicitly requested. The batch is sequential by default, records each item's status, and can resume with `--run-dir <existing-run-dir>`.
7. Treat `notes/` as the user-facing output. Treat `run.json`, `source/`, and `items/` as provenance and recovery evidence.

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
contract_version: 1
platform: youtube
source_url: original YouTube page URL
source_id: YouTube video ID
author: channel name
title: video title
published_at: original time when available
captured_at: local capture time
scope: single | creator_recent_n
transcript_source: platform_caption | asr | unavailable | null
caption_language: language | null
caption_type: manual | automatic | translated | null
asr_status: pending | skipped | completed | failed | cancelled
asr_model: model | null
task_id: task ID | null
media_delivery: oss-signed-url | null
status: running | captured | partial | failed | pending_review | cancelled
review_status: unreviewed | sampled | needs_review
stages:
  <stage>:
    status: pending | running | succeeded | skipped | failed | cancelled
    started_at: RFC3339 | null
    completed_at: RFC3339 | null
    attempt: integer
    retryable: true | false
    error: string | null
    artifact_paths: []
    reason: string | null
```

The same contract is used for single-video notes and each channel-batch item. `stages` is stored in the single-video `manifest.json` and in each item inside the batch `run.json`; it is not just console logging. `null` means that a field does not apply or has not been reached yet. `captured` means the transcript was produced and is non-empty; it does not mean the content has been fact-checked. A usable platform caption sets `transcript_source=platform_caption`, `asr_status=skipped`, and records its language/type. If no usable caption exists, the pipeline falls back to ASR and records `transcript_source=asr`. Automatic translated captions are not used by default.

For a single video, the output directory contains:

```text
note.md
manifest.json
metadata.json
video/
  captions/
    <language>.<type>.browser.txt
    selection.json
  transcript.md
  request-info.json
  oss.json
  submit.json
  task.json
  transcription.json
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

- A Computer Use export is also a platform-caption source: retain the returned UTF-8 .txt file, record retrieval_method=computer_use under the manifest captions object, and set transcript_source=platform_caption plus asr_status=skipped. The browser export path itself is not written into the Markdown body.
- Computer Use does not grant the local Python process the Chrome login state. It is a separate, read-only browser route; if the current environment cannot control an authorized Chrome tab, report that prerequisite instead of silently claiming a caption capture.

- For a single video, yt-dlp is used only to download a local audio file after Computer Use confirms that no usable Transcript exists. It is never used to extract the single video's captions or to resolve a YouTube CDN URL for Bailian.
- The ASR route always uploads the local media file to the private OSS Bucket and gives Bailian a short-lived signed GET URL. The signed URL is never persisted.
- `qwen-audio-3.0-asr-flash-filetrans` accepts one publicly reachable media URL per task and supports long media within the service's documented limits. The result URL is temporary and is downloaded immediately into `video/transcription.json`.
- By default the Skill does not read the Chrome browser profile. For yt-dlp routes, if the fixed external Cookie directory contains a supported file, the helper passes that file automatically; explicit `--cookies <path>` overrides it. The Skill never writes Cookie contents to artifacts. Cookie use may expose the account to YouTube rate limits or account risk, so use it only when necessary. The Skill still does not bypass login, CAPTCHA, age gates, or access controls. Private, members-only, region-blocked, age-restricted, live, or deleted videos may fail.
- If Chrome's Windows DPAPI prevents `--cookies-from-browser chrome`, keep using the exported Mozilla/Netscape-format file in the fixed external directory, or explicitly pass another path with `--cookies <path>`. Refresh the file only when yt-dlp reports that the login/session is no longer accepted.
- A batch completing means the pipeline finished its per-item work. It does not mean the transcript is factually correct; manually sample notes before using them as research material.
- Channel batches are intentionally sequential. This limits accidental API bursts and makes partial recovery understandable. Do not start a second batch for the same channel while one is running.
- Never persist `DASHSCOPE_API_KEY`, raw signed media URLs, or raw signed transcription-result URLs.
- OSS 清理默认是 dry-run；只有显式 `--execute` 加精确确认字符串才会删除对象。清理报告只保存对象键、大小和时间，不保存密钥或签名 URL。

## Individual debugging commands

```powershell
# check dependencies
yt-dlp --version
python "<skill-root>\scripts\youtube_video_to_md.py" --help
python "<skill-root>\scripts\youtube_channel_to_md.py" --help

# only when yt-dlp needs an explicit YouTube login source for metadata/audio download
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "<youtube-video-url>" `
  --cookies-from-browser chrome

# alternative when Chrome Cookie decryption fails; keep the file outside the repo
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "<youtube-video-url>" `
  --cookies "$env:TEMP\youtube-cookies.txt"

# use a browser-confirmed no-Transcript result and go directly to OSS ASR
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "<youtube-video-url>" `
  --browser-no-transcript `
  --browser-title "<visible-video-title>" `
  --browser-author "<visible-channel-name>"

# continue an interrupted batch
python "<skill-root>\scripts\youtube_channel_to_md.py" `
  "<youtube-channel-url>" `
  --run-dir ".\youtube-channel-results\<existing-run>"

# preview temporary OSS objects older than one day
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1
```
