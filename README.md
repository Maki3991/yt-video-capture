# YouTube Video Capture

把指定的 YouTube 视频转成带时间戳的 Markdown 文字稿，也可以指定一个公开频道，按频道 `/videos` 列表的顺序处理前 N 条视频。

本 Skill 借用了现有小红书 Skill 的核心结构：

```text
页面链接 → yt-dlp 临时媒体直链 → 百炼异步 ASR → 原始 JSON 证据 + Markdown
```

YouTube 不需要小红书的 Chrome Bridge。第一版也不把视频下载到本地，而是让百炼读取 yt-dlp 临时解析出的媒体 URL。

## 一次性准备

需要：Windows、Python 3、`yt-dlp` 和百炼 API Key。默认不读取浏览器 Cookie。

```powershell
yt-dlp --version
$env:DASHSCOPE_API_KEY = "只在当前 PowerShell 会话中设置，不要写入文件"
```

如果 `yt-dlp` 不在 PATH 中，可以设置路径：

```powershell
$env:YOUTUBE_YTDLP = "D:\path\to\yt-dlp.exe"
```

也可以给命令加 `--yt-dlp "D:\path\to\yt-dlp.exe"`。脚本还会兼容已经用于小红书的 `XHS_YTDLP` 环境变量。

如果 YouTube 返回 “Sign in to confirm you’re not a bot”，可以在明确理解风险后，仅对当前运行显式加：

```powershell
--cookies-from-browser chrome
```

也可以指定 Chrome 配置文件，例如 `--cookies-from-browser "chrome:Default"`。脚本不会把 Cookie 保存到仓库或输出目录；不要把这个选项当成默认配置。yt-dlp 官方也提醒，使用账号批量请求可能触发限流或账号风险。

百炼默认使用：

```text
qwen-audio-3.0-asr-flash-filetrans
```

## 单个视频

建议先用一个视频做真实验证：

```powershell
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "https://www.youtube.com/watch?v=<video-id>" `
  --out-dir ".\youtube-video-results\one-video"
```

成功后查看：

```text
youtube-video-results/one-video/note.md
youtube-video-results/one-video/manifest.json
youtube-video-results/one-video/video/transcript.md
```

如果只看到 `note.md` 但 `manifest.json` 是 `failed`，那是失败证据，不是完成的文字稿。

## 频道前 N 条

先做 5 条小批次：

```powershell
python "<skill-root>\scripts\youtube_channel_to_md.py" `
  "https://www.youtube.com/@<channel>/videos" `
  --limit 5 `
  --run-name "channel-pilot"
```

确认小批次后再改成 50：

```powershell
python "<skill-root>\scripts\youtube_channel_to_md.py" `
  "https://www.youtube.com/@<channel>" `
  --limit 50 `
  --run-name "channel-first-50"
```

最终 Markdown 在批次目录的 `notes/`；`source/` 保存频道清单，`items/` 保存每条视频的元数据、任务和转写结果。

中断后继续：

```powershell
python "<skill-root>\scripts\youtube_channel_to_md.py" `
  "https://www.youtube.com/@<channel>" `
  --run-dir ".\youtube-channel-results\channel-first-50-<时间>"
```

## 可选参数

- `--language-hints zh,en`：已知视频语言时给百炼提示；不填则自动判断。
- `--cookies-from-browser chrome`：明确允许 yt-dlp 读取浏览器登录态，用于 YouTube 的 bot/login challenge；默认关闭。
- `--diarization`：请求说话人分离；长视频先不要默认开启。
- `--channel-tab shorts`：批量采集 Shorts；默认只采集 `/videos`。
- `--delay 1`：批量视频之间的等待时间，默认 1 秒。
- `--timeout 7200`：单个异步转写任务最长等待时间。

## 当前限制

- YouTube CDN 的临时直链不保证百炼服务器一定能访问；失败时保留错误和原始链接，不伪造文字稿。
- YouTube 对自动化请求会持续调整反爬、PO Token 和登录要求；“给任意视频链接都成功”不是当前可承诺的能力。先用一个真实链接验收，再扩大批量。
- 不读取浏览器 Cookie，不绕过登录、年龄限制、验证码、地区限制或付费访问。
- “前 N 条”是本次 `yt-dlp` 看到的公开频道标签顺序，不代表频道所有历史视频，也不包括不可访问条目。
- 批量任务按顺序处理，50 条视频可能需要较长时间并产生相应 ASR 费用；先验证单条和 5 条小批次。
- 文字稿是 ASR 派生结果，使用前应抽查原视频；采集成功不等于内容事实已经核验。
