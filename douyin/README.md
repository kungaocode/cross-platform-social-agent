# Douyin adapter

Browser transport and an optional hybrid backend/browser workflow are supported.
Backend private messaging currently handles bound personal text conversations.
Browser publishing verifies the actual @ target and comment result.

- [Transport and capability boundaries](../docs/platforms.md#douyin)
- [Local setup](../docs/configuration.md#douyin-api-setup)
- [Optional upstream environment](../third_party/README.md)
- [Upstream attribution](THIRD_PARTY_LICENSE.txt)

The default browser configuration and `config.api.local.json` are different
entry points. Select an API configuration explicitly with `--config`.
Account bindings, cookies and security material stay in private local storage.

抖音支持网页与后台混合接入。后台私信、视频取材和网页评论分别处理；遇到登录
或验证要求时需人工处理。不要将旧网页适配器的群/多媒体能力等同于后台收件能力。
