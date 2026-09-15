# YouTube Video Capture

把指定的 YouTube 视频转成带时间戳的 Markdown 文字稿，也可以指定一个公开频道，按频道 `/videos` 列表的顺序处理前 N 条视频。

本 Skill 借用了现有小红书 Skill 的核心结构：

```text
页面链接 → Computer Use 登录 Chrome → 有 Transcript → 导出字幕 → Markdown
页面链接 → Computer Use 确认无 Transcript → yt-dlp 下载本地音频 → 私有 OSS → 百炼异步 ASR → Markdown
```

单视频统一使用当前 Computer Use 可控制的、用户已登录的 Chrome 页面判断是否存在 Transcript。若有，就导出字幕并保存本地 Markdown；若没有，就使用 yt-dlp 下载本地音频，再上传私有 OSS，最后交给百炼 ASR。单视频不再使用 yt-dlp 提取字幕，也不再把 YouTube CDN 直链交给百炼。

## 一次性准备

需要：Windows、Python 3 和 `yt-dlp`。有浏览器 Transcript 的视频只需要 Computer Use；没有 Transcript 时还需要百炼 API Key、`oss2` 和私有 OSS Bucket。默认不读取浏览器 Cookie。

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

如果 Windows Chrome 的 DPAPI 无法解密 Cookie，可改用 `--cookies "$env:TEMP\youtube-cookies.txt"` 传入你自己导出的 Mozilla/Netscape 格式 Cookie 文件。Cookie 文件高度敏感，应放在仓库外，任务完成后删除；脚本只在当前运行中把路径传给 yt-dlp，不会写入 Markdown、JSON 或 manifest。

百炼默认使用：

```text
qwen-audio-3.0-asr-flash-filetrans
```

## 单个视频

### 当前登录 Chrome 的 Computer Use 路径

在 Computer Use 中选择用户已经打开并登录的 Chrome YouTube 标签页，导航到视频链接。如果有广告，等待“跳过广告”按钮出现后点击，不要固定假设广告一定持续多少秒。然后调用该标签页的 YouTube transcript export 能力。它会返回一个 UTF-8 `.txt` 文件路径。把这个路径交给同一个单视频脚本：

~~~powershell
python "<skill-root>\scripts\youtube_video_to_md.py" "<youtube-video-url>" --browser-transcript-file "<computer-use-exported-txt>" --browser-title "<visible-video-title>" --out-dir ".\youtube-video-results\browser-one-video"
~~~

这个模式只导入浏览器已经导出的字幕，不运行 yt-dlp、OSS 或百炼。输出的 manifest.json 会在 captions 对象中记录 retrieval_method: computer_use。

如果页面没有可用 Transcript，使用浏览器页面上可见的标题和频道名，明确告诉脚本进入 ASR 路径：

~~~powershell
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "<youtube-video-url>" `
  --browser-no-transcript `
  --browser-title "<visible-video-title>" `
  --browser-author "<visible-channel-name>" `
  --out-dir ".\youtube-video-results\one-video-asr"
~~~

此时脚本不会检查 yt-dlp 字幕，也不会解析 YouTube 直链；它会用 yt-dlp 下载本地音频，再上传 OSS 并调用百炼。如果 Computer Use 无法控制已登录 Chrome，不能假设视频有或没有字幕；应报告这个前置条件，或在用户明确确认后处理已知的无字幕视频。

建议先用一个视频做真实验证：

```powershell
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "https://www.youtube.com/watch?v=<video-id>" `
  --browser-transcript-file "<computer-use-exported-txt>" `
  --browser-title "<visible-video-title>" `
  --out-dir ".\youtube-video-results\one-video"
```

成功后查看：

```text
youtube-video-results/one-video/note.md
youtube-video-results/one-video/manifest.json
youtube-video-results/one-video/video/transcript.md
```

浏览器字幕路径输出还会包含：

```text
youtube-video-results/one-video/video/captions/<language>.<type>.browser.txt
youtube-video-results/one-video/video/captions/selection.json
```

此时 `manifest.json` 和 `note.md` 中应为 `transcript_source: platform_caption`、`asr_status: skipped`，不会提交百炼 ASR。

如果只看到 `note.md` 但 `manifest.json` 是 `failed`，那是失败证据，不是完成的文字稿。

### 无 Transcript 时通过私有 OSS 交给百炼

先安装 SDK：

```powershell
python -m pip install oss2
```

如果已经有本地音频（例如 `source.m4a`），可直接测试上传和转写：

```powershell
python "<skill-root>\scripts\youtube_video_to_md.py" `
  "https://www.youtube.com/watch?v=<video-id>" `
  --media-file ".\local-audio\source.m4a" `
  --out-dir ".\youtube-video-results\one-video-oss"
```

使用 `--browser-no-transcript` 时，不提供 `--media-file`，脚本会先用 yt-dlp 下载音频到输出目录，再上传 OSS。OSS Bucket 保持私有，脚本只把短时签名 URL 交给百炼，不把签名 URL 写入 Markdown 或 JSON。`--media-file` 仅用于复用已有本地音频，跳过重复下载。

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

## 统一输出契约

单视频的 `note.md` 和频道批次中每个视频条目使用同一组核心字段：

```yaml
contract_version: 1
platform: youtube
source_url: 原始视频链接
source_id: 视频 ID
author: 作者或频道
title: 标题
published_at: 发布时间或 null
captured_at: 本地采集时间
scope: single | creator_recent_n | date_range
transcript_source: platform_caption | asr | unavailable | null
caption_language: 语言或 null
caption_type: manual | automatic | translated | null
asr_status: pending | skipped | completed | failed | cancelled
asr_model: 模型或 null
task_id: 百炼任务 ID 或 null
media_delivery: oss-signed-url | null
status: running | captured | partial | failed | pending_review | cancelled
review_status: unreviewed | sampled | needs_review
```

有可用平台字幕时，结果标记为 `transcript_source: platform_caption`、`asr_status: skipped`，并记录字幕语言和类型；没有可用字幕时才标记为 `transcript_source: asr`。`captured` 只表示文字稿非空且处理完成，不代表内容已经人工核验。自动翻译字幕默认不作为原始文字稿。

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

- --browser-transcript-file <path>：导入 Computer Use 从登录 YouTube 页面导出的 UTF-8 文字稿；不能与 Cookie、--metadata-file 或 --media-file 一起使用。
- --browser-no-transcript：表示 Computer Use 已确认页面没有可用 Transcript，直接进入本地音频→OSS→百炼；可配合 `--browser-title` 和 `--browser-author`。
- --browser-title <title>：补充浏览器页面可见的视频标题。
- --browser-author <author>：可选，补充浏览器页面可见的频道名。
- `--language-hints zh,en`：已知视频语言时给百炼提示；不填则自动判断。
- `--cookies-from-browser chrome`：明确允许 yt-dlp 读取浏览器登录态，用于 YouTube 的 bot/login challenge；默认关闭。
- `--cookies <path>`：显式使用 Mozilla/Netscape 格式 Cookie 文件，作为 Chrome DPAPI 失败时的替代；默认关闭，且不与 `--cookies-from-browser` 同时使用。
- `--diarization`：请求说话人分离；长视频先不要默认开启。
- `--media-file <path>`：使用已有本地音频，跳过 yt-dlp 下载；单视频 ASR 始终上传 OSS。
- `--oss-object-key <key>`：自定义 OSS 对象路径；默认是 `youtube-asr/<视频ID>.<扩展名>`。
- `--oss-url-expires <seconds>`：OSS 签名 URL 有效期，默认 3600 秒。
- `--channel-tab shorts`：批量采集 Shorts；默认只采集 `/videos`。
- `--delay 1`：批量视频之间的等待时间，默认 1 秒。
- `--timeout 7200`：单个异步转写任务最长等待时间。

## 当前限制

- 浏览器字幕路线依赖当前 Computer Use 能控制用户已经登录的 Chrome 标签页；普通 Python 进程不会自动继承浏览器登录态。
- 无 Transcript 路线仍需要 yt-dlp 成功下载本地音频；如果 YouTube 对 yt-dlp 返回登录/反爬挑战，下载阶段仍会失败，需要用户明确提供可用的 Cookie 方案或本地音频。
- OSS 路径需要有效的 RAM AccessKey、`oss2` 和 Bucket Policy；上传失败或签名 URL 失效时同样只记录失败证据。
- YouTube 对自动化请求会持续调整反爬、PO Token 和登录要求；“给任意视频链接都成功”不是当前可承诺的能力。先用一个真实链接验收，再扩大批量。
- 不读取浏览器 Cookie，不绕过登录、年龄限制、验证码、地区限制或付费访问。
- “前 N 条”是本次 `yt-dlp` 看到的公开频道标签顺序，不代表频道所有历史视频，也不包括不可访问条目。
- 批量任务按顺序处理，50 条视频可能需要较长时间并产生相应 ASR 费用；先验证单条和 5 条小批次。
- 文字稿可能来自 YouTube 字幕或 ASR；两者都应抽查原视频，采集成功不等于内容事实已经核验。
