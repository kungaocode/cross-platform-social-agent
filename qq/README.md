# QQ adapter

Official QQ bot SDK → private Node/Python bridge → common agent core → original
conversation reply route. Supports text, images, speech, MP4 and common documents.

- [Setup and private files](../docs/configuration.md)
- [Document support and group rules](../docs/platforms.md#qq)
- [Privacy and publication](../docs/privacy.md)

Start with `python run_qq.py --check`, then `python run_qq.py`. Configure the
bot's platform permissions and local allowlists first. Account files live in
`private/accounts/qq/`; environment credentials are also supported.

QQ适配使用官方机器人身份。文档与微信共用公共解析模块；真实配置和凭据仅留
本地。群文件是否免 @ 推送仍受 QQ 权限限制，详见上述说明。
