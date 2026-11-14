# Privacy and publication / 隐私与发布

## Source versus local state

The repository contains source, tests, sanitized templates and documentation.
Keys and account files belong in `private/`; chat databases, browser profiles,
downloads, screenshots, reports and generated jobs remain local runtime state.
Active configuration files can contain contact names and account identifiers,
so they are ignored even after their key files have been separated.

Existing deployments retain active configuration and external-memory paths to
avoid interrupting a running listener. Their files are excluded from Git.
The local `private/migration-manifest.json` records archived paths and hashes;
it is not a public artifact. `private/archive/` contains historical notes and
backups, which may contain personal information or obsolete credentials.

`private/` is a filesystem and version-control boundary, not encryption.
Control access to the machine and backups as you would for other account data.

## Before committing

```powershell
git status --short
python scripts/check_publication.py
python scripts/check_publication.py --history
git diff --cached --stat
```

The check inspects the actual Git index. It rejects private/runtime paths,
unreviewed binaries, common token formats and known values read from local key,
account and active configuration files. It prints finding categories and
locations, not the sensitive values. `--head` checks the committed snapshot.
`--history` checks all reachable commits, including deleted files and commit
messages. Logs, traces, traffic captures and raw line-oriented exports are
excluded from publication even when their extension differs from `.log`.
Use `git config core.hooksPath .githooks` to enable the local pre-commit guard.

This check is an additional guard, not a guarantee of exhaustive detection.
Review newly added fixtures, screenshots, provider responses and logs before
sharing. Do not force-add ignored files. GitHub repository privacy does not
make publishing credentials safe.

## If a credential is accidentally committed

Stop distributing that snapshot, revoke or rotate the exposed credential,
and remove it from affected history and copies. Adding the file to `.gitignore`
after a commit does not remove previous copies.

## 中文说明

源码、模板和公开说明进入仓库；密钥、账号安全材料、真实配置、聊天记录、
浏览器资料、报告、截图及个人媒体留本地。`private/` 不等于加密，仍需妥善
管理本机与备份权限。历史归档可能含个人信息，整个目录都不上传。

发布检查扫描实际暂存内容，只输出问题位置和类别。它还会把待提交文本与本机
已知敏感值比对；缺少本机私密文件的干净环境只能进行格式和路径检查。
`--history` 会检查全部可达提交，包含后来被删除的文件及提交说明；轮转日志、
JSONL/NDJSON、追踪文件和抓包文件同样排除，不能仅凭扩展名不是 `.log` 就上传。
不要把“仓库是私有的”当作上传凭据的理由，也不要用强制添加绕开忽略规则。
