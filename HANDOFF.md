# YouTube 视频采集 Skill｜开发 Handoff

> 日期：2026-09-14  
> 交给：负责继续开发 `yt-video-capture` 的 Agent  
> 目标：在进入频道小批量采集前，完成“字幕优先、无字幕再 ASR、失败可解释、异步任务可续跑”的 YouTube 单视频与批量基础。

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
YouTube URL
  → yt-dlp 获取 metadata
  → yt-dlp 解析临时媒体直链
  → 百炼异步 ASR
  → note.md + video/transcript.md + JSON 证据
```

当前实现的几个事实：

- 目前没有 YouTube 字幕优先分支；即使视频已有字幕，也会继续走 ASR。
- `metadata.json` 由 yt-dlp 的 JSON 清洗而来，不包含待长期保存的签名媒体直链。
- 默认不把完整视频下载到本地，只把 yt-dlp 解析出的临时媒体 URL 交给百炼。
- 当前单视频成功输出 `note.md`、`metadata.json`、`manifest.json` 和 `video/` 证据目录。
- 当前频道批量已经有 `source/channel.json`、`source/videos.json`、`run.json`、逐项 `items/` 和 `notes/`，并能跳过已有成功项、使用 `--run-dir` 继续。
- 当前 ASR 已保存 `request-info.json`、`resolution.json`、`submit.json`、`task.json`、`transcription.json`，但重新启动时还不会根据已有 `task_id` 继续轮询。
- 当前实现使用 `--ignore-config`，默认不读 Cookie；只有用户显式提供 `--cookies-from-browser` 时才使用浏览器登录态。

## 2. 总优先级

以下优先级必须保持，不要先做低优先级的目录美化或总控路由。

### P0：现在必须完成

#### P0-1｜字幕优先，再决定是否 ASR

这是新增的第一道分流，必须发生在 ASR 提交之前。

目标流程：

```text
获取 metadata
  → 检查可用字幕
  → 有可用原始字幕：下载/保存字幕并跳过 ASR
  → 没有可用字幕：解析媒体直链并进入 ASR
  → 统一渲染 Markdown
```

字幕优先级建议：

1. 用户指定语言的人工/原始字幕；
2. 用户指定语言的自动生成字幕；
3. 没有可用原始字幕时进入 ASR；
4. 自动翻译字幕不默认作为原始文字稿使用。若用户明确允许，必须标记为 `translated`，不能伪装成原始字幕。

实现时使用 yt-dlp 的字幕能力，例如检查可用字幕、选择语言、下载人工字幕和自动字幕。不要把字幕 URL 当作永久来源；下载后的 `.vtt`/`.srt` 原文件才是字幕证据。

字幕路径的输出至少应包含：

```text
video/
  captions/
    <language>.<vtt|srt>       # 原始字幕文件
  transcript.md                # 清洗后的统一文字稿
```

并在 manifest/frontmatter 中记录：

```yaml
transcript_source: platform_caption
caption_language: en
caption_type: manual | automatic | translated
asr_status: skipped
```

字幕下载、解析或内容为空时，不得标记为字幕成功；可以回退到 ASR，并同时记录字幕阶段失败原因。

#### P0-2｜阶段状态与失败记录

这是五个已讨论点中的第一优先级，也是后续续跑的基础。

每个视频至少拆成这些阶段：

```text
metadata
captions
media_resolve
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

本次不要求自动实现本地下载/OSS 备用链路；如果临时直链无法被百炼访问，先按阶段记录失败。备用媒体投递可以作为后续独立闸门。

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
platform: youtube
source_url: original YouTube page URL
source_id: YouTube video ID
author: channel name
title: video title
published_at: original publish time | unknown
captured_at: local capture time
scope: single | creator_recent_n | date_range
transcript_source: platform_caption | asr | unavailable
caption_language: language | null
caption_type: manual | automatic | translated | null
asr_model: model | null
task_id: task ID | null
status: captured | partial | failed | pending_review | cancelled
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

用一个真实且确认有可用字幕的视频：

- metadata 成功；
- 原始 `.vtt`/`.srt` 保存；
- 生成规范化 `transcript.md` 和 `note.md`；
- `transcript_source=platform_caption`；
- ASR 没有提交，或 manifest 明确记录 `asr=skipped`；
- 字幕语言和类型可见。

### Gate B：ASR 回退路径

用一个确认没有可用字幕的视频：

- 字幕阶段记录 `no_usable_caption`；
- yt-dlp 成功解析媒体直链；
- 百炼任务成功并保存 task checkpoint；
- 生成带时间戳的 `transcript.md` 和 `note.md`；
- `transcript_source=asr`；
- `manifest` 中可以回溯模型、task_id 和证据路径。

### Gate C：失败与续跑

至少验证：

- metadata 失败；
- 字幕下载/解析失败后回退 ASR；
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
- 不默认下载完整视频；媒体下载应是明确动作或后续备用链路；
- 不读取、保存或输出 Cookie、API Key、签名媒体 URL；
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
