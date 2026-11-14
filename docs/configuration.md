# Local configuration / 本地配置

Run `python scripts/init_local.py` from a fresh checkout. It copies public
templates, creates a common memory directory, and leaves existing files intact.
Generated paths are absolute so platform-specific relative-path conventions do
not split memory into different directories.

| Local file | Contents |
| --- | --- |
| `private/secrets/api_deepseek.txt` | DeepSeek key on the first non-comment line |
| `private/secrets/api_qwen.txt` | Qwen vision/PDF key; an optional compatible endpoint can also be specified |
| `private/secrets/api_audio.txt` | Qwen ASR/video key and, if needed, its service endpoint |
| `private/secrets/weather_api.txt` | AMap weather key; environment variables can be used instead |
| `private/secrets/search_credentials.json` | Search provider credentials, following the public example |
| `private/accounts/qq/credentials.json` | Bot `appId` and `appSecret`; alternatively use `QQBOT_APPID` / `QQBOT_SECRET` |
| `private/accounts/bilibili/credentials.json` | Account session material, including `SESSDATA` and `bili_jct` when sending is needed |
| `private/accounts/douyin/credentials-api.json` | Optional API account binding and security material, obtained through the local setup workflow |
| `config.json` | Common models, shared memory, WeChat scope and tools |
| `qq/config.json`, `bilibili/config.json`, `douyin/config.json` | Local platform configuration and allowlists |
| `douyin/config.api.local.json` | Optional Douyin hybrid transport configuration |
| `scheduler.json` | Cross-process model request limits |

All files in this table are ignored by Git. Do not put real values in the
versioned `*.example.json` files.

## Models and features

The common configuration selects the final conversation model, task model and
perception providers. `runtime.agent_memory_enabled` and
`runtime.agent_memory_path` enable a shared memory store. Keep platform memory
directories and inherited base configuration aligned.

Read-only and action tools have independent switches. The template deliberately
leaves WeChat tools off until configured. Configure the relevant tool switches
before expecting weather, search, file or platform operations to be available.
Shared memory does not by itself enable sending or identify two accounts as the
same person.

QQ document reading is controlled by `document_reading_enabled`. Group file
events also follow `group_multimodal_global`. PDF files are limited to 150 MiB,
other documents to 30 MiB; a message may contain up to two attachments.
Office visual analysis requires LibreOffice on PATH.

## Douyin API setup

The initializer creates an inactive API configuration from
`douyin/config.api.example.json`. Install the optional isolated backend as
described in [third-party setup](../third_party/README.md). Establish the
project browser profile through manual login, then use `run_douyin_api.py`
`export` / `setup` / `bind` / `status` to prepare the same account's local
credentials. Consult `python run_douyin_api.py --help` for commands.

Only enable writes after account and contact bindings have been verified.
`setup` may need a manual visit to an existing personal conversation; normal
message receiving/sending uses the backend. Browser comment publishing uses
the dedicated profile and checks the selected @ target and publication result.
Login or verification challenges still require manual handling.

Existing deployments may keep their API credentials and profiles beside an
external memory store. Keep that location private; moving it is not required
to publish source code. The active API configuration must explicitly select the
intended credential path. Never copy runtime-generated job configurations into
version control.

## 中文说明

首次克隆后运行初始化脚本，再填写 `private/secrets/` 和
`private/accounts/`。已有配置不会被覆盖。各端白名单、账号绑定、模型配置、
登录资料和写操作开关需要分别准备；启用共享记忆不会自动开启发布或发送权限。

实际配置和模板分开保存：只提交模板，实际 `config.json` 仍留本地。已有项目在
外置目录保存的抖音凭据、浏览器资料和记忆库继续保持原路径，也不得上传。
抖音 API 配置与默认网页配置是两个入口，启动时需要明确选择。
