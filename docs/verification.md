# Verification snapshot

Date: 2026-11-14. Environment: Windows, Python 3.12, installed project dependencies.

- Exported the proposed Git index into a separate clean directory, without
  local account files, memory stores or vendored backend sources.
- Full Python regression: **676 tests passed** in that clean directory.
- Installed QQ dependencies from the lockfile with lifecycle scripts disabled;
  **14 Node tests passed**.
- Initialization created local configuration and empty key files; a second run
  created zero files. The three platform memory-directory settings aligned.
- Local QQ configuration/SDK, Bilibili configuration and Douyin browser
  configuration checks passed after credential-path separation. These checks
  did not send messages or validate remote account sessions.
- The publication guard passed against the staged snapshot, including known
  local key/account values. Its regression verifies rejection without printing
  the sensitive value.

A recommendation-order test fixture was corrected to explicitly return no
existing comment from its registry mock. Previously the mock's default truthy
return caused all candidates to be skipped. Production video-selection behavior
was not changed for this correction.

These results verify reproducible local behavior, not full real-account or
online-model acceptance. Runtime reports and personal test material remain
outside the published tree.

中文：本次从 Git 暂存内容导出干净目录验证，676 项 Python 和 14 项 Node 测试
通过；初始化可重复运行且不覆盖已有配置；敏感内容检查通过。真实账号收发、
在线模型和平台验证要求仍需分别验收。
