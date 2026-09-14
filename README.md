# YouTube Video Capture

把指定的 YouTube 视频转成带时间戳的 Markdown 文字稿，也可以指定一个公开频道，按频道 `/videos` 列表的顺序处理前 N 条视频。

本 Skill 借用了现有小红书 Skill 的核心结构：

```text
页面链接 → yt-dlp 临时媒体直链 → 百炼异步 ASR → 原始 JSON 证据 + Markdown
页面链接 → 本地音频 → 私有 OSS 短时签名 URL → 百炼异步 ASR → 原始 JSON 证据 + Markdown
```

YouTube 不需要小红书的 Chrome Bridge。默认路径不下载完整视频；如果百炼无法读取 YouTube CDN，可显式使用 `--via-oss`，先把音频上传到私有 OSS，再把短时签名 URL 交给百炼。

## 一次性准备

需要：Windows、Python 3、`yt-dlp`、`oss2`（使用 OSS 路径时）和百炼 API Key。默认不读取浏览器 Cookie。

```powershell
yt-dlp --version
$env:DASHSCOPE_API_KEY = "只在当前 PowerShell 会话中设置，不要写入文件"
```

使用 OSS 路径时，将 `.env.example` 复制为 Skill 目录下的 `.env`，填写 RAM 子账号 `codex-youtube-asr` 的 `OSS_ACCESS_KEY_ID` 和 `OSS_ACCESS_KEY_SECRET`。`.env` 已被 Git 忽略，不要把密钥发到聊天或提交到仓库。

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

### 通过私有 OSS 交给百炼

先安装 SDK：

```powershell
python -m pip install oss2
```

如果已经有本地音频（例如 `source.m4a`），可直接测试上传和转写：

```powershell
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "https://www.youtube.com/watch?v=<video-id>" `
  --via-oss `
  --media-file ".\test-results\<run>\video\source.m4a" `
  --out-dir ".\youtube-video-results\one-video-oss"
```

不提供 `--media-file` 时，脚本会先用 yt-dlp 下载音频到输出目录，再上传 OSS。OSS Bucket 保持私有，脚本只把短时签名 URL 交给百炼，不把签名 URL 写入 Markdown 或 JSON。

### 清空 OSS 内存

“清空 OSS 内存”指清理本 Skill 上传的临时文件，不是删除整个 Bucket。OSS 的每个对象都有 `last_modified` 时间，脚本每次运行时用“当前 UTC 时间减去保留天数”计算截止时间，再找出更早的对象。

脚本不会常驻后台；想每天检查时，可以用 Windows 任务计划程序每天启动它。先预览：

```powershell
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1 `
  --report ".\cleanup-results\preview.json"
```

默认只列出候选对象，不删除。确认预览列表后，才运行删除：

```powershell
python "<skill-root>\scripts\oss_cleanup.py" `
  --prefix "youtube-asr/" `
  --older-than-days 1 `
  --execute `
  --confirm "DELETE youtube-asr/" `
  --report ".\cleanup-results\delete.json"
```

删除保护要求同时提供 `--execute` 和精确确认字符串，并且脚本拒绝清理 `youtube-asr/` 之外的路径。交互式使用时，应先看候选对象，再明确确认本次删除。

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
- `--via-oss`：走“本地音频 → 私有 OSS → 百炼”路径，绕开 YouTube CDN 对百炼的可达性问题。
- `--media-file <path>`：配合 `--via-oss` 使用已有本地音频，跳过重复下载。
- `--oss-object-key <key>`：自定义 OSS 对象路径；默认是 `youtube-asr/<视频ID>.<扩展名>`。
- `--oss-url-expires <seconds>`：OSS 签名 URL 有效期，默认 3600 秒。
- `--channel-tab shorts`：批量采集 Shorts；默认只采集 `/videos`。
- `--delay 1`：批量视频之间的等待时间，默认 1 秒。
- `--timeout 7200`：单个异步转写任务最长等待时间。

## 当前限制

- YouTube CDN 的临时直链不保证百炼服务器一定能访问；失败时保留错误和原始链接，不伪造文字稿。
- OSS 路径需要有效的 RAM AccessKey、`oss2` 和 Bucket Policy；上传失败或签名 URL 失效时同样只记录失败证据。
- YouTube 对自动化请求会持续调整反爬、PO Token 和登录要求；“给任意视频链接都成功”不是当前可承诺的能力。先用一个真实链接验收，再扩大批量。
- 不读取浏览器 Cookie，不绕过登录、年龄限制、验证码、地区限制或付费访问。
- “前 N 条”是本次 `yt-dlp` 看到的公开频道标签顺序，不代表频道所有历史视频，也不包括不可访问条目。
- 批量任务按顺序处理，50 条视频可能需要较长时间并产生相应 ASR 费用；先验证单条和 5 条小批次。
- 文字稿是 ASR 派生结果，使用前应抽查原视频；采集成功不等于内容事实已经核验。
