# YouTube 视频采集 Skill｜开发 Handoff

> 日期：2026-09-14  
> 交给：负责继续开发 `yt-video-capture` 的 Agent  
> 目标：完成“浏览器字幕优先、无字幕走本地音频→OSS→百炼、失败可解释、异步任务可续跑”的 YouTube 单视频基础，并为频道批量复用。

## 0. 阅读顺序和当前边界

先阅读：

1. `docs/AI 信息采集工程.md`
2. `yt-video-capture/SKILL.md`
3. `yt-video-capture/README.md`
4. `yt-video-capture/scripts/youtube_video_to_md.py`
5. `yt-video-capture/scripts/youtube_channel_to_md.py`
6. `yt-video-capture/scripts/youtube_asr.py`

本次只处理 YouTube。不要扩展到 X/Twitter、抖音、Crawl4AI、全网搜索、研究总控或事实核验。

当前项目采用受控来源采集：用户给出单个视频链接，或明确给出频道、频道标签和数量/时间范围。不要默认搜索频道、相关推荐或全网视频。

## 1. 当前代码的真实状态

当前 `yt-video-capture` 的单视频路径是：

```text
YouTube URL + 当前 Codex 可控制的登录 Chrome
  → Computer Use 完成广告清除门槛并确认主视频开始
  → 有可用 Transcript：导出页面文字稿并生成 Markdown
  → 无可用 Transcript：yt-dlp 下载本地音频
  → 私有 OSS 短时签名 URL
  → 百炼异步 ASR
  → note.md + video/transcript.md + JSON 证据
```

当前实现的几个事实：

- 当前单视频脚本已经提供 `--browser-transcript-file` 浏览器导入入口：校验 Computer Use 导出的 YouTube 视频 ID，保存浏览器原始文本，生成统一 Markdown，并在 manifest 的 captions 中记录 `retrieval_method=computer_use`；该路径不调用 yt-dlp、OSS 或百炼。
- 当前单视频脚本已经提供 `--browser-no-transcript` 分流信号：跳过 yt-dlp 字幕检查，使用 yt-dlp 下载本地音频，再强制通过私有 OSS 交给百炼。
- 单视频不再使用 yt-dlp 提取字幕，也不再解析或提交 YouTube CDN 直链。
- `metadata.json` 可以来自浏览器导入、浏览器提供的标题/作者，或显式复用的 metadata 文件；不保存签名媒体 URL。
- 浏览器字幕成功时，单视频输出 `video/captions/<caption-type>.browser.txt`、`selection.json` 和规范化 `transcript.md`，不需要百炼 API Key。
- 真实视频 `CzDTaLqozlQ` 和 `subGeHuQ_lY` 已通过 CUA 登录页面确认能导出字幕，并已导入为统一 Markdown；该路径已真实通过。
- 当前单视频成功输出 `note.md`、`metadata.json`、`manifest.json` 和 `video/` 证据目录。
- 当前频道批量已经有 `source/channel.json`、`source/videos.json`、`run.json`、逐项 `items/` 和 `notes/`，并能跳过已有成功项、使用 `--run-dir` 继续。
- 当前单视频和频道批量条目已经共享 v1 输出契约：来源、范围、字幕字段、ASR 字段、处理状态和人工复核状态；字幕成功时 `transcript_source=platform_caption`、`asr_status=skipped`。
- P0-2 阶段状态记录已接入：单视频 `manifest.json` 和频道 `run.json` 的每个 item 都持久化 `metadata`、`browser_transcript`、`media_download`、`oss_upload`、`asr_submit`、`asr_poll`、`transcript_download`、`markdown_render` 八个阶段，以及状态、时间、attempt、retryable、错误和产物路径；ASR 内部阶段在每次变化时立即写回。
- 当前 ASR 已保存 `request-info.json`、`oss.json`、`submit.json`、`task.json`、`transcription.json`，但重新启动时还不会根据已有 `task_id` 继续轮询。
- 当前共享 ASR 层只接受本地媒体文件，固定使用本地音频 → 私有 OSS → 百炼链路；`--via-oss` 和 YouTube CDN 直链路径已从单视频入口移除。
- 当前实现使用 `--ignore-config`，不读取 Chrome 配置文件；如果 PATH 中有 Node.js，会自动追加 `--js-runtimes node` 处理 YouTube JavaScript challenge。yt-dlp 路线会自动检查外部固定目录 `D:\Softwares\Programming Projects\_yt-cookies\` 中的 Cookie 文件，显式 `--cookies <path>` 优先；`--cookies-from-browser` 与 `--cookies` 仍只能选一个，后者用于 Chrome DPAPI 无法解密时的 Mozilla/Netscape Cookie 文件。

## 2. 总优先级

以下优先级必须保持，不要先做低优先级的目录美化或总控路由。

### P0：现在必须完成

#### P0-1｜浏览器字幕优先，无字幕再走本地 OSS ASR

单视频的分流由 Skill 层的 Computer Use 完成，不再由 yt-dlp 检查字幕：

```text
打开用户已登录的 Chrome
  → 广告门槛：至少观察 10 秒，每 2～3 秒检查，最长 60 秒
  → 有“跳过广告”就点击；没有按钮就等待自然结束
  → 确认广告控件消失、主视频画面出现，再等待约 3 秒
  → 尝试导出 YouTube Transcript
  → 导出成功：浏览器导入器生成 Markdown，并跳过 ASR
  → 没有 Transcript：传入 --browser-no-transcript
  → yt-dlp 下载本地音频
  → 私有 OSS 短时签名 URL
  → 百炼异步 ASR
  → 统一渲染 Markdown
```

约束：

- Transcript 导出前必须通过广告门槛；不能因为 Transcript 面板可打开就跳过广告检查；
- 导出后检查文字稿开头，疑似广告时丢弃并重做门槛，最多重试 2 次；
- 60 秒仍无法确认主视频开始时记录 `ad_not_cleared`，不得把广告字幕标记为成功；
- Python 单视频脚本不调用 yt-dlp 字幕能力；
- Python 单视频脚本不把 YouTube CDN 直链交给百炼；
- `--browser-transcript-file` 表示字幕已由 Computer Use 导出；
- `--browser-no-transcript` 表示 Computer Use 已确认没有可用 Transcript；
- ASR 分支必须先得到本地媒体文件，再上传私有 OSS；
- 浏览器导入成功时记录 `transcript_source=platform_caption`、`asr_status=skipped`；
- ASR 成功时记录 `transcript_source=asr`、`media_delivery=oss-signed-url`。

#### P0-2｜阶段状态与失败记录（代码已实现，Gate C 待验收）

这是五个已讨论点中的第一优先级，也是后续续跑的基础。

每个视频至少拆成这些阶段：

```text
metadata
browser_transcript
media_download
oss_upload
asr_submit
asr_poll
transcript_download
markdown_render
```

每个阶段至少记录：

```yaml
status: pending | running | succeeded | skipped | failed | cancelled
started_at: RFC3339
completed_at: RFC3339 | null
attempt: 1
retryable: true | false
error: null | string
artifact_paths: []
```

失败记录不是普通日志。它必须能回答：哪条来源、哪一步失败、是否可重试、留下了什么产物、下一步做什么。

要求：

- 顶层状态不得把 `partial` 或 `failed` 当作 `captured`；
- 失败时仍保留原始 YouTube URL、metadata 和已完成阶段的产物；
- 单项失败不应让频道批量丢失其他已确认项；
- 不在错误中保存 Cookie、API Key、签名媒体 URL 或签名转写结果 URL；
- 批量层继续维护成功、失败、跳过、不支持、待处理数量。

实现状态：本轮已经把八个阶段写入单视频 `manifest.json` 和频道 `run.json` 的 item，并在阶段变化后立即持久化；频道 `run.json` 同步维护各状态计数。尚未完成的是“中断后恢复原 task_id”的 P1-1，以及 Gate C 对真实中断场景的验收。

### P1：P0 通过后、频道批量前完成

#### P1-1｜ASR 任务支持续跑

这是五个点中的第二优先级。

核心机制：

```text
提交 ASR 后立即保存 task_id
  → 程序中断
  → 重新运行时读取 task_id
  → 若任务仍在运行：继续轮询原任务
  → 若任务成功但结果未落盘：只下载转写结果并渲染
  → 只有没有有效 task_id，或任务明确失败，才新建 attempt 再提交
```

具体要求：

- `task_id` 必须在提交成功后立即持久化；
- 重启时不能因为已有 `task_id` 就重新提交任务；
- 已经 `SUCCEEDED` 的任务不能重复计费；
- 转写结果下载失败时，优先重试结果下载，不重新提交 ASR；
- 新的 ASR 提交必须保留 `attempt` 编号和旧任务状态；
- API Key 继续只从当前进程环境读取，不写入任何产物；
- 原始转写 JSON 和规范化 Markdown 都要保留。

建议沿用 `video/request-info.json`、`video/submit.json`、`video/task.json` 和 `video/transcription.json`，但让它们成为真正可恢复的 checkpoint，而不只是调试文件。

OSS 投递链路已经作为单视频 ASR 的固定路径实现；本阶段不把 OSS 清理或媒体投递续跑与 ASR 任务续跑混在一起。OSS→百炼的真实端到端成功仍需单独验收，不能用模拟测试代替。

#### P1-2｜不覆盖旧批次，恢复必须显式

这是五个点中的第三优先级。

规则：

- 默认运行创建新的单视频目录或批次目录；
- 指定的已有目录如果不是显式恢复，拒绝运行或自动创建明确的 `-run-N` 新目录；
- 只有 `--run-dir <existing-run-dir>` 才能继续旧批次；
- 恢复前校验原始频道 URL、频道标签、数量/时间范围和批次配置一致；
- 不静默覆盖旧的 metadata、字幕、ASR 结果或 Markdown；
- 重试产物使用 attempt 或阶段目录，不覆盖已保存的失败证据。

“新建批次”和“继续旧批次”必须是用户能够看懂的两种操作，不要根据文件是否存在来偷偷猜测用户意图。

#### P1-3｜冻结频道清单，再处理视频

这是五个点中的第四优先级，且当前代码已经部分实现。

“冻结”是保存本次任务要处理的视频快照，不等于必须让用户逐条确认。

两种模式都要支持：

```text
直接批量模式：用户明确给出频道 + tab + limit
  → 自动生成 source/videos.json
  → 直接处理这份清单
```

```text
候选确认模式：用户要求先挑选视频，或范围不清楚
  → 生成清单
  → 等用户选择/确认
  → 只处理确认后的清单
```

对于“处理这个频道最近 5 条视频”，不需要再次询问；但必须固定本次看到的 5 条。清单至少记录：

```yaml
source_url: channel URL
tab: videos | shorts | streams
requested_count: 5
collected_count: 5
enumerated_at: RFC3339
collection_order: yt-dlp result order for this run
items: source_id, source_url, title, position
```

恢复时复用已有 `source/videos.json`，不要重新读取频道后改变条目顺序。频道发生变化时，创建新批次。

### P2：最后整理

#### P2-1｜用户文件与证据文件分层

这是五个点中的第五优先级，当前代码已经有雏形，可以最后完善。

建议保持：

```text
notes/       用户阅读的最终 Markdown
source/      频道和视频清单
items/       每条视频的 metadata、字幕、ASR 和错误证据
run.json     批次状态和计数
failures.json 批次失败摘要
```

单视频则保持：

```text
note.md              用户阅读入口
metadata.json        视频 metadata
manifest.json        状态、来源和产物索引
video/               字幕/ASR 原始证据及 transcript.md
```

用户平时只需要打开 `notes/` 或 `note.md`；开发 Agent 和后续重试通过 `source/`、`items/`、`manifest` 查证。最终 Markdown 不能隐藏失败，也不能把 AI 总结冒充原始字幕或 ASR 原文。

## 3. 统一输出契约

无论文字稿来自字幕还是 ASR，最终都应保留同一组基础字段：

```yaml
contract_version: 1
platform: youtube
source_url: original YouTube page URL
source_id: YouTube video ID
author: channel name
title: video title
published_at: original publish time | unknown
captured_at: local capture time
scope: single | creator_recent_n | date_range
transcript_source: platform_caption | asr | unavailable | null
caption_language: language | null
caption_type: manual | automatic | translated | null
asr_status: pending | skipped | completed | failed | cancelled
asr_model: model | null
task_id: task ID | null
media_delivery: oss-signed-url | null
status: running | captured | partial | failed | pending_review | cancelled
review_status: unreviewed | sampled | needs_review
```

规则：

- 有可用字幕且成功解析：`transcript_source=platform_caption`，`asr_status=skipped`；
- 无可用字幕且 ASR 成功：`transcript_source=asr`；
- 字幕失败后 ASR 成功：保留字幕阶段失败，并标记最终来源为 `asr`；
- 字幕和 ASR 都失败：`transcript_source=unavailable`，不能生成“成功文字稿”；
- `note.md` 和 `video/transcript.md` 可以共享规范化文本，但不能重复制造不同版本的正文；
- 签名 URL 只在内存中短暂使用，持久化 JSON 只保留脱敏后的 URL/主机信息。

## 4. 验收闸门

### Gate A：字幕路径

浏览器路线的额外验收要求：

- 广告门槛通过后才导出 Transcript，并能确认导出开头不是广告内容；
- Computer Use 从用户已登录的 Chrome 页面导出 UTF-8 文字稿；
- 浏览器导出的原始文本保存到 video/captions/；
- captions 中记录 retrieval_method=computer_use；
- 该次运行不调用 yt-dlp、OSS 或百炼。

用一个真实且确认有可用字幕的视频：

- metadata 成功；
- 浏览器导出的原始 `.txt` 保存；
- 生成规范化 `transcript.md` 和 `note.md`；
- `transcript_source=platform_caption`；
- ASR 没有提交，或 manifest 明确记录 `asr=skipped`；
- 字幕语言和类型可见。

### Gate B：ASR 回退路径

用一个确认没有可用字幕的视频：

- Computer Use 记录 `no_usable_transcript`；
- yt-dlp 成功下载本地音频；
- `oss.json` 记录上传对象；
- 百炼任务成功并保存 task checkpoint；
- 生成带时间戳的 `transcript.md` 和 `note.md`；
- `transcript_source=asr`；
- `media_delivery=oss-signed-url`，且 `manifest` 中可以回溯模型、task_id 和证据路径；
- 提交给百炼的输入不是 YouTube CDN 直链。

### Gate C：失败与续跑

至少验证：

- metadata 失败；
- yt-dlp 本地音频下载失败时保留失败证据；
- ASR 任务提交后程序中断，再运行时继续原 task；
- 转写结果下载失败时不重新提交任务；
- 失败项生成清晰的阶段状态和错误记录；
- 失败 Markdown 明确写明“不是完整文字稿”。

### Gate D：频道小批量

Gate A-C 通过后，再运行频道 5 条小批量：

- 先固定 `source/videos.json`；
- 逐项处理并更新 `run.json`；
- 中断后使用 `--run-dir` 继续；
- 已成功项不重复 ASR；
- 新运行不覆盖旧运行；
- 最终报告成功、失败、跳过和待重试数量；
- 人工抽查字幕路径和 ASR 路径各至少一条。

## 5. 开发 Agent 的执行纪律

- 先完成 P0，再做 P1，最后整理 P2；
- 不把静态检查、CLI 帮助或模拟任务写成真实端到端成功；
- 不把“有字幕”推断成字幕一定完整可用；
- 不把 ASR 文字稿当作事实核验结果；
- 无 Transcript 时只下载音频到本地用于 OSS 投递；不默认保存完整视频；
- 不读取 Chrome 配置数据库；仅按固定目录或显式路径读取已导出的 Cookie 文件，不保存或输出 Cookie 内容、API Key 或签名媒体 URL；
- 不为补足频道数量而搜索新视频；
- 不在本次任务中开发跨平台总控；
- 每完成一个 Gate，都更新 `README.md`/项目导航中的状态、证据等级、已知失败和下一闸门，但不要修改与本次 YouTube 任务无关的文件。

## 6. 参考

- 本项目导航：`docs/AI 信息采集工程.md`
- 当前 Skill：`yt-video-capture/SKILL.md`
- 当前 README：`yt-video-capture/README.md`
- `yichen-content-archive` 的输入/动作/归档边界：
  <https://raw.githubusercontent.com/mcncarl/yichen-skills/main/yichen-content-archive/SKILL.md>
- `yichen-content-archive` 的 YouTube 路由：
  <https://raw.githubusercontent.com/mcncarl/yichen-skills/main/yichen-content-archive/references/platform-routes.md>
- yt-dlp：<https://github.com/yt-dlp/yt-dlp>
