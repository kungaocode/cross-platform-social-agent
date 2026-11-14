# Platform implementation notes / 平台实现说明

## WeChat

`social_bot/platforms/wechat/` owns client automation and platform operations.
The public core owns models, storage, scope, document analysis, search and
weather. `run_wechat.py` starts chat listening; `run_moments.py` runs the
separate Moments workflow. Client compatibility, current chat routing and
platform permissions must be checked on the actual machine.

## QQ

`qq/index.mjs` receives official bot SDK events and validates scope before
queueing. `qq/bridge.mjs` preserves attachment metadata and reply targets.
`social_bot/platforms/qq/worker.py` repeats allowlist checks, reads attachments,
and passes bounded evidence to the common conversation core.

Documents reuse the same `DocumentModule` as WeChat. Supported extensions:
PDF, DOCX, XLS/XLSX, PPTX, TXT, MD, CSV, JSON, XML, HTML and HTM. Old DOC/PPT
files must first be converted. Text extraction feeds the task model; PDF and
visual Office analysis use the file-perception path. Unsupported, encrypted,
damaged or unreadable files produce an explicit failure. Model-analysis
failure may fall back to extracted text, with its limitations stated.

Private files can be sent directly. Group files follow the global-media switch,
and require @ when that switch is disabled. Platform permissions determine
whether non-mention group events are actually delivered. Results support
follow-up within the bounded conversation history; unseen sections require a
new file message with a specific question.

Qzone publishing is an optional, separately authorized service. It is not
automatically provided by a QQ bot connection.

## Bilibili

`run_bilibili.py` provides account checks, search, video metadata, per-part
subtitles, Q&A and private-message listening. Subtitle Q&A and audiovisual
analysis are separate paths. `run_video_social.py` manages browsing, evidence,
sharing and the limited tracking of comments from a task. Mention notifications
and replies have independent configuration switches.

## Douyin

The browser adapter remains available. The hybrid workflow combines:

1. `api_inbox.py` and `api_runner.py`: receive new, bound personal-text
   conversations, persist events and send replies with server-message receipts.
2. `api_video_commands.py`: parse fixed video commands, bind the sender and
   manage progress, interpretations and completion messages.
3. Backend candidate/media acquisition plus light or deep evidence analysis.
4. `browser_comment.py`: select a real @ entity in the page editor, publish,
   and read back the comment result.

Light mode reads titles and sampled frames without ASR or independent review.
Deep mode adds the configured evidence and audit pipeline. Current capabilities
must be read from the chosen configuration; an image/video flag inherited from
the browser configuration does not make the API inbox accept media.

The API inbox currently excludes groups, images, voice messages and shared
video cards. Reconnection restores subscription, not offline history. Comment
notification monitoring is not implemented in that API mode. Unknown sends
and publications are not automatically repeated.

## Shared core

- `service.py`: scope, conversation context, tools, memory retrieval and replies.
- `deepseek.py`: task/conversation model handoff.
- `store.py`, `agent_memory.py`: local delivery/history state and shared memory.
- `reply_jobs.py`: persisted reply task progression and supported recovery.
- `scheduler.py`: cross-process model request limits.
- `video_*.py`, `deep_video.py`: browsing, evidence, analysis and task reports.
- `modules/`: shared perception and utility modules.

## 中文说明

四端负责各自接入和平台动作，模型、记忆、搜索、天气和公共文档处理集中维护。
微信覆盖综合社交与群操作；QQ覆盖官方机器人聊天、多媒体和常用文件；B站覆盖
视频阅读与社区互动；抖音采用后台私信和取材、网页评论的混合方案。

实现路径、配置开关、进程正在运行、真实账号验收通过是不同状态。尤其需要
区分抖音网页能力与后台收件能力、视频轻读与深度理解、字幕问答与音画理解，
以及生成文案和确认发送/发布。
