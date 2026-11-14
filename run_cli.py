from __future__ import annotations

import argparse
import json

from social_bot.service import BotService
from social_bot.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="本地测试 DeepSeek 社交消息服务")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    service = BotService(load_settings(args.config))
    print("输入 JSON 消息进行测试，输入 quit 退出。")
    print('示例：{"platform":"qq","chat_type":"private","chat_id":"u1","sender_id":"u1","text":"你好"}')
    try:
        while True:
            line = input("> ").strip()
            if line.lower() in {"quit", "exit"}:
                break
            try:
                result = service.handle(json.loads(line))
                print(json.dumps(result, ensure_ascii=False))
            except json.JSONDecodeError as exc:
                print(json.dumps({"accepted": False, "reason": "invalid_json", "error": str(exc)}, ensure_ascii=False))
    finally:
        service.close()


if __name__ == "__main__":
    main()
