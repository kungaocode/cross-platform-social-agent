from __future__ import annotations

import argparse
import logging
import time

from social_bot.service import BotService
from social_bot.settings import load_settings
from social_bot.wechat import WeChatAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek 微信个人号自动回复")
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出调试日志，含每条被忽略消息的原因（配置白名单时很有用）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(args.config)
    if not settings.wechat.enabled:
        raise SystemExit("config.json 的 wechat.enabled 当前为 false，请先改为 true")

    service = BotService(settings)
    adapter = WeChatAdapter(service, settings.wechat)
    if settings.scope.group_require_mention and not settings.wechat.bot_name:
        print(
            "⚠️ 未设置 wechat.bot_name：群聊里任何人 @ 别人都会被当成在叫机器人。"
            "建议填成你自己的微信显示名。"
        )
    try:
        client = adapter.start()
    except Exception as exc:  # noqa: BLE001 - show a short actionable startup message
        adapter.close()
        service.close()
        raise SystemExit(f"微信适配器启动失败：{exc}") from exc
    print(
        "微信适配器已启动。目标："
        + ", ".join(settings.wechat.targets)
        + f"；回复模式：{settings.wechat.reply_mode}"
        + f"；工具调用：{'开启' if settings.runtime.tool_calling_enabled else '关闭'}"
        + f"；消息处理线程：{settings.runtime.message_workers}"
        + f"；撤回工具：{'开启' if settings.runtime.allow_recall_tool else '关闭'}"
    )
    if adapter.failed_targets:
        print(
            "⚠️ 以下目标没能挂上监听（其余正常）："
            + "、".join(f"{name}（{reason}）" for name, reason in adapter.failed_targets)
        )
    if settings.scope.group_activation_enabled:
        # 关键词以 config.json 为准，不要写死，否则提示与实际行为不一致。
        print(
            f"群聊先发送“{settings.scope.group_activate_keyword}”接通，"
            f"再由群成员 @ {settings.wechat.bot_name or '机器人'} 提问；"
            f"发送“{settings.scope.group_deactivate_keyword}”停止该群。"
        )
        print("控制词你自己发也生效；@提问只响应别人——避免机器人回复自己造成刷屏。")
    try:
        keep_running = getattr(client, "KeepRunning", None)
        if callable(keep_running):
            keep_running()
        else:
            # wxauto4 41.x 使用 StartListening/StopListening，没有 KeepRunning。
            start = getattr(client, "StartListening", None)
            if callable(start):
                start()
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop = getattr(client, "StopListening", None)
        if callable(stop):
            stop()
        adapter.close()
        service.close()


if __name__ == "__main__":
    main()
