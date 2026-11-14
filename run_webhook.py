from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from social_bot.service import BotService
from social_bot.settings import load_settings


class Handler(BaseHTTPRequestHandler):
    service: BotService

    def _write_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write_json(200, {"ok": True, "paused": self.service.paused})
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/message":
            self._write_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            self._write_json(200, self.service.handle(data))
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"accepted": False, "reason": "invalid_request", "error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("webhook").info(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek 社交消息统一入口")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = BotService(load_settings(args.config))
    Handler.service = service
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Webhook 已启动：http://{args.host}:{args.port}/message")
    print("健康检查：http://127.0.0.1:%s/healthz" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
