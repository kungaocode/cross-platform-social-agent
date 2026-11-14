from __future__ import annotations

import argparse
import json
import logging

from social_bot.deepseek import DeepSeekClient
from social_bot.modules.moments import MomentsModule, MomentsRunError, MomentsRunner
from social_bot.modules.weather import WeatherModule
from social_bot.settings import load_settings
from social_bot.wechat import WeChatAdapter, _load_wechat_class, ensure_main_window_ready


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek 独立朋友圈定时发布")
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出朋友圈流程调试日志",
    )
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt", help="本次运行临时覆盖 moments.template")
    prompt_group.add_argument("--prompt-file", help="从 UTF-8 文本文件读取本次运行的提示词")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="只读诊断：打印朋友圈可见内容、控件树摘要和解析出的评论，不发布任何东西",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="单次模式：发布一条并发出开篇评论后立刻退出，不轮询评论、不发结束文案",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(args.config)
    if not settings.moments.enabled:
        raise SystemExit("config.json 的 moments.enabled 当前为 false，请先改为 true")

    client = None
    moments = None
    try:
        # 这里只创建微信客户端和朋友圈窗口，不调用 AddListenChat，和聊天机器人进程完全分离。
        ensure_main_window_ready()
        client = WeChatAdapter._create_client(_load_wechat_class())
        moments = MomentsModule(lambda: client)
        if args.diagnose:
            # 只读诊断：打开朋友圈、读取可见内容和评论控件结构，不发布、不点击。
            report = moments.diagnose()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        model = DeepSeekClient(settings.api_key, settings.deepseek)
        moments_settings = settings.moments
        if args.prompt is not None:
            from dataclasses import replace

            moments_settings = replace(moments_settings, template=args.prompt.strip())
        elif args.prompt_file:
            from dataclasses import replace
            from pathlib import Path

            prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
            moments_settings = replace(moments_settings, template=prompt)
        if not moments_settings.template:
            raise SystemExit("朋友圈提示词不能为空")
        try:
            account_nickname = str(getattr(client, "nickname", "") or "").strip()
        except Exception:  # noqa: BLE001 - 取账号昵称失败不影响主流程
            account_nickname = ""
        runner = MomentsRunner(
            model,
            moments,
            moments_settings,
            weather=WeatherModule(
                timeout_seconds=settings.runtime.weather_timeout_seconds,
                provider=settings.runtime.weather_provider,
                ip_cache_file=settings.runtime.weather_cache_file,
            ),
            # 自己的评论在微信里可能显示成「我」，也可能显示成账号昵称，两个都传进去。
            bot_names=(settings.wechat.bot_name, account_nickname),
        )
        print(f"朋友圈天气模块：{settings.runtime.weather_provider}")
        if not moments_settings.weather_location:
            print("朋友圈天气定位：未固定城市，按 IP 定位（高德 /v3/ip，失败时回退上次成功位置）")
        single_shot = args.once or not moments_settings.wait_for_comments
        if single_shot:
            print("朋友圈运行模式：单次模式（发布一条 + 发出开篇评论后退出，不轮询评论、不发结束文案）")
        else:
            print(
                f"朋友圈独立模式已启动：首条发布后等待 {moments_settings.duration_minutes} 分钟，"
                "到时发布结束文案。"
            )
        print(
            "评论回应方式："
            + (
                "在自己朋友圈的评论区发一条普通评论（Comment 接口）"
                if moments_settings.comment_reply_mode != "reply"
                else "点击原评论做点对点回复（依赖 wxauto 暴露评论控件）"
            )
        )
        result = runner.run(once=args.once)
        print(f"首条朋友圈：{result['opening']}")
        if result.get("media_dropped"):
            print(f"注意：配图发布失败，这条朋友圈是纯文字版本（{result['media_dropped']}）")
        print(f"目标追踪：{'已定位到自己发布的这条朋友圈' if result.get('tracked') else '未定位到'}")
        opening_comment = result.get("opening_comment")
        if opening_comment is not None:
            print(
                "开篇说明评论："
                + ("已用 Comment 接口发出" if opening_comment.get("success") else f"发送失败（{opening_comment.get('reason')}）")
            )
        if result.get("once"):
            print("单次模式已完成：未等待评论、未发布结束文案。")
        else:
            print(f"期间回应评论：{result['replied_comments']} 条")
            print(f"结束文案：{result['closing']}")
    except KeyboardInterrupt:
        print("已手动停止朋友圈独立模式，结束文案不会自动补发。")
    except (MomentsRunError, RuntimeError, OSError) as exc:
        raise SystemExit(f"朋友圈独立模式启动失败：{exc}") from exc
    finally:
        if moments is not None:
            try:
                moments.close()
            except Exception:  # noqa: BLE001 - 关闭窗口失败不应覆盖主错误
                logging.getLogger(__name__).exception("关闭朋友圈窗口失败")


if __name__ == "__main__":
    main()
