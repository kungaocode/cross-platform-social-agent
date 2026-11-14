"""Bilibili account, search and DeepSeek video Q&A command line."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from social_bot.deepseek import DeepSeekClient, DeepSeekError
from social_bot.settings import load_settings
from social_bot.store import ConversationStore
from social_bot.platforms.bilibili.client import BilibiliClient, BilibiliError
from social_bot.platforms.bilibili.assistant import VideoAssistant

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="B站视频阅读、私信与@评论回复")
    parser.add_argument("--config", type=Path, default=ROOT / "bilibili/config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "account"):
        commands.add_parser(name)
    commands.add_parser("inbox")
    commands.add_parser("sessions", help="只读列出当前账号聊天列表的个人会话 UID")
    commands.add_parser("listen").add_argument("--once", action="store_true", help="只扫描基线一轮，不回复历史消息")
    commands.add_parser("search").add_argument("keyword")
    for name in ("video", "subtitles", "ask", "chat"):
        sub = commands.add_parser(name)
        sub.add_argument("video")
        sub.add_argument("--page", type=int)
        if name == "ask":
            sub.add_argument("question", nargs="?", default="请总结视频的主要内容和关键结论。")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    credentials_file = config_path.parent / config["credentials_file"]
    saved = json.loads(credentials_file.read_text(encoding="utf-8-sig")) if credentials_file.exists() else {}
    token = os.environ.get("BILIBILI_SESSDATA", saved.get("SESSDATA", ""))
    if not isinstance(token, str):
        raise ValueError("SESSDATA 必须是字符串")
    api = BilibiliClient(token.strip())
    if args.command == "account":
        account = api.account()
        print(json.dumps(account, ensure_ascii=False, indent=2))
        return
    if args.command == "sessions":
        from social_bot.platforms.bilibili.social import BilibiliSocialClient
        peers = BilibiliSocialClient(token.strip()).sessions()
        print(json.dumps(dict(peer_count=len(peers), peers=peers), ensure_ascii=False, indent=2))
        return
    if args.command == "search":
        print(json.dumps(api.search(args.keyword), ensure_ascii=False, indent=2))
        return
    if args.command in ("video", "subtitles"):
        detail = api.video(args.video, args.page)
        print(json.dumps(detail if args.command == "video" else api.subtitles(detail), ensure_ascii=False, indent=2))
        return
    settings = load_settings((config_path.parent / config["base_config"]).resolve())
    memory = Path(config["memory_dir"])
    if not memory.is_absolute():
        memory = config_path.parent / memory
    if not memory.is_dir():
        raise ValueError("记忆目录不存在")
    if args.command == "check":
        from social_bot.platforms.bilibili.chat import validate_config
        validate_config(config)
        from social_bot.platforms.common.chat import platform_settings
        platform_settings(settings, "bilibili", config, memory / "check.sqlite3")
        with tempfile.TemporaryFile(dir=memory) as f:
            f.write(b"check")
        print("配置、模型密钥文件和记忆目录检查通过；SESSDATA " + ("已配置（未联网验证）" if token else "未配置，使用匿名模式"))
        print("聊天发信凭据 bili_jct：" + ("已配置（未联网验证）" if os.environ.get("BILIBILI_CSRF", saved.get("bili_jct", "")) else "未配置，监听发信需要补充"))
        return
    if args.command in ("listen", "inbox"):
        from social_bot.platforms.bilibili.chat import run_social
        csrf = os.environ.get("BILIBILI_CSRF", saved.get("bili_jct", ""))
        return run_social(config_path, settings, memory, token.strip(), csrf,
                          once=getattr(args, "once", False), inbox=args.command == "inbox")
    account = api.account() if token else {"logged_in": False}
    if token and not account["logged_in"]:
        raise BilibiliError("LOGIN_REQUIRED")
    identity = account.get("uid") if account["logged_in"] else "anonymous"
    if identity != "anonymous" and not (identity and identity.isdigit()):
        raise BilibiliError("BAD_ACCOUNT_RESPONSE")
    store = ConversationStore.from_runtime(settings.runtime,database_path=memory / f"bilibili-{identity}.sqlite3",account_id=identity)
    assistant = VideoAssistant(api, DeepSeekClient(settings.api_key, settings.deepseek), store, identity)
    try:
        if args.command == "ask":
            print(assistant.ask(args.video, args.question, args.page))
        else:
            print("本地视频问答，输入 /exit 结束；不会向 B 站发送私信或评论。")
            while True:
                question = input("你：").strip()
                if question == "/exit":
                    break
                if question:
                    print(assistant.ask(args.video, question, args.page))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (BilibiliError, ValueError, OSError) as exc:
        # Never print cookie files, raw HTTP bodies or credential-bearing request headers.
        print("执行失败：" + (exc.code if isinstance(exc, BilibiliError) else "请检查参数、配置文件和目录"))
        raise SystemExit(1)
    except DeepSeekError:
        print("模型调用失败：请检查 DeepSeek 网络、额度和配置；未保存本轮回答。")
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("已退出。")
