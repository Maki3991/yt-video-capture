---
name: yt-video-capture
description: Convert a specified YouTube video, or the first N public videos from a specified YouTube channel, into traceable local Markdown transcripts using yt-dlp and Bailian asynchronous ASR. Do not use it for platform publishing, unrestricted discovery, or bypassing access controls.
---

# YouTube Video Capture

This Skill has two entry points:

- one YouTube video URL → one Markdown transcript;
- one public YouTube channel URL + N → the first N entries from the selected public channel tab, each archived as Markdown.

The default media route is:

```text
YouTube page URL
  → yt-dlp resolves a temporary audio/video URL
  → Bailian qwen-audio-3.0-asr-flash-filetrans asynchronous task
  → timestamped transcript + Markdown note + JSON evidence
```

It deliberately does not reuse the XHS browser extension. YouTube public video and channel pages are handled by yt-dlp; the XHS Skill keeps its existing logged-in Chrome capture path.

## Workflow

1. One-time setup: ensure Python 3, `yt-dlp`, and a Bailian API key are available. Put the key only in the current process environment as `DASHSCOPE_API_KEY`. Do not write it into the repository, Markdown, or JSON artifacts. The default route does not read browser cookies.
2. For one video, run:

   ```powershell
   python "<skill-root>\scripts\youtube_video_to_md.py" `
     "<youtube-video-url>" `
     --out-dir ".\youtube-video-results\one-video"
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
- The main route does not download the full video to local disk and does not require FFmpeg or OSS. If Bailian cannot fetch the resolved YouTube CDN URL, the Skill records failure; it does not silently invent a transcript.
- `qwen-audio-3.0-asr-flash-filetrans` accepts one publicly reachable media URL per task and supports long media within the service's documented limits. The result URL is temporary and is downloaded immediately into `video/transcription.json`.
- By default the Skill does not read browser cookies. If the user explicitly supplies `--cookies-from-browser`, yt-dlp reads that browser profile for the current run only; the Skill never saves the cookies. This may expose the account to YouTube rate limits or account risk, so use it only when necessary. The Skill still does not bypass login, CAPTCHA, age gates, or access controls. Private, members-only, region-blocked, age-restricted, live, or deleted videos may fail.
- A batch completing means the pipeline finished its per-item work. It does not mean the transcript is factually correct; manually sample notes before using them as research material.
- Channel batches are intentionally sequential. This limits accidental API bursts and makes partial recovery understandable. Do not start a second batch for the same channel while one is running.
- Never persist `DASHSCOPE_API_KEY`, raw signed CDN URLs, or raw signed transcription-result URLs.

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
```
