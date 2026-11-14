"""QQ 聊天发布说说：固定命令、独立授权、持久化发布收据。"""
from __future__ import annotations

import json
import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener, ProxyHandler

from ...cache import cache_task
from .media import QQMediaReader, attachment_kind


@dataclass(frozen=True)
class PublishCommand:
    mode: str
    payload: str


def match_publish_command(text):
    text = text.strip()
    block = re.fullmatch(r"\[(?:QQ说说|QQ空间)\]\s*(复制|生成)[：:]([\s\S]+)", text)
    if block and block[2].strip():
        return PublishCommand("copy" if block[1] == "复制" else "generate", block[2])
    natural = re.fullmatch(r"(?:请|帮我|麻烦)?(?:发(?:一条|个)?|发布)(?:QQ空间动态|QQ空间说说|说说)[：:]([\s\S]+)", text)
    if natural and natural[1].strip():
        return PublishCommand("auto", natural[1].strip())
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("qzone_redirect_rejected")


class QzonePublisher:
    MAX_IMAGES = 9
    MAX_IMAGE_BYTES = 12 * 1024 * 1024
    MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024

    def __init__(self, connection, config=None, transport=None, image_downloader=None):
        self.db = connection
        self.config = config or {}
        self.transport = transport or self._post
        self.image_downloader = image_downloader or QQMediaReader._download
        self.db.execute("CREATE TABLE IF NOT EXISTS qq_qzone_publications (event_key TEXT PRIMARY KEY, state TEXT NOT NULL, receipt TEXT)")
        self.db.commit()

    def authorized(self, sender, kind):
        return (self.config.get("enabled") is True and kind == "c2c"
                and sender in self.config.get("owner_openids", []))

    def _post(self, payload):
        base = self.config.get("base_url", "").rstrip("/")
        parsed = urlsplit(base)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}))):
            raise ValueError("qzone_base_url_invalid")
        token = os.environ.get(self.config.get("token_env", "QZONE_ACCESS_TOKEN"), "").strip()
        if not token:
            raise ValueError("qzone_token_missing")
        request = Request(base + "/send_qzone_msg", data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
        # 写操作只发一次，不使用带代理重试的 net.urlopen，也不跟随跳转。
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=90 if payload.get("images") else 30) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("qzone_response_too_large")
        return json.loads(data)

    def _prepare_images(self, attachments):
        images, total = [], 0
        if not attachments:
            return images
        # 使用现有 QQ 图片下载校验：HTTPS 白名单、公网地址、禁止跳转、格式及大小检查。
        # 缓存沿用项目托管缓存策略，由 clean_cache.py 统一清理。
        with cache_task("qq-qzone") as folder:
            for index, attachment in enumerate(attachments):
                target = Path(folder) / str(index)
                target.mkdir()
                path = self.image_downloader(attachment.get("url", ""), str(target), image=True)
                with Path(path).open("rb") as source:
                    data = source.read(self.MAX_IMAGE_BYTES + 1)
                total += len(data)
                if not data or len(data) > self.MAX_IMAGE_BYTES or total > self.MAX_TOTAL_IMAGE_BYTES:
                    raise ValueError("qzone_image_size_limit")
                # base64 可跨机器传给发布服务，不要求双方有相同本地路径。
                images.append("base64://" + base64.b64encode(data).decode("ascii"))
        return images

    def _save_receipt(self, event_key, state, result):
        with self.db:
            self.db.execute("UPDATE qq_qzone_publications SET state=?, receipt=? WHERE event_key=?", (state, json.dumps(result), event_key))

    def publish(self, event_key, sender, kind, command, arguments, *, attachments=None):
        if not self.authorized(sender, kind):
            return {"success": False, "reason": "qzone_owner_private_chat_required"}
        if command is None or arguments.get("confirm") is not True:
            return {"success": False, "reason": "explicit_qzone_publish_command_required"}
        text = command.payload if command.mode == "copy" else arguments.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return {"success": False, "reason": "qzone_text_required_max_2000"}
        attachments = [] if attachments is None else attachments
        if (not isinstance(attachments, list) or len(attachments) > self.MAX_IMAGES
                or any(not isinstance(item, dict) or attachment_kind(item) != "image" for item in attachments)):
            return {"success": False, "reason": "qzone_images_only_max_9"}
        # 可见范围只接受部署配置，模型不能扩大公开范围。
        rights = self.config.get("ugc_right", 4)
        if rights not in (1, 4, 64):
            return {"success": False, "reason": "qzone_visibility_invalid"}
        if not self.config.get("base_url") or not os.environ.get(self.config.get("token_env", "QZONE_ACCESS_TOKEN")):
            return {"success": False, "reason": "qzone_service_not_configured"}
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO qq_qzone_publications VALUES (?, 'preparing', NULL)", (event_key,)).rowcount
        if not inserted:
            row = self.db.execute("SELECT state, receipt FROM qq_qzone_publications WHERE event_key=?", (event_key,)).fetchone()
            if row[0] == "published":
                return {**json.loads(row[1]), "cached": True}
            return {"success": False, "reason": "qzone_result_unknown_no_retry" if row[0] in ("preparing", "sending", "unknown") else "qzone_publish_failed_no_retry"}
        try:
            images = self._prepare_images(attachments)
        except Exception:
            result = {"success": False, "reason": "qzone_image_prepare_failed", "side_effect_performed": False,
                      "detail": "图片未能全部准备完成，说说未发布；请检查图片来源、格式及大小后重新发送。"}
            self._save_receipt(event_key, "failed", result)
            return result
        payload = {"content": text, "ugc_right": rights}
        if images:
            payload["images"] = images
        with self.db:
            self.db.execute("UPDATE qq_qzone_publications SET state='sending' WHERE event_key=?", (event_key,))
        state = "unknown"
        try:
            data = self.transport(payload)
            if data.get("status") == "ok" and data.get("retcode") == 0:
                state = "published"
                result = {"success": True, "action": "qzone_publish", "side_effect_performed": True,
                          "message_sent": False, "visibility": rights,
                          "image_count": len(images),
                          "tid": str((data.get("data") or {}).get("tid") or "")}
            elif isinstance(data.get("retcode"), int) and data["retcode"] != 0:
                state = "failed"
                result = {"success": False, "reason": "qzone_api_rejected", "retcode": data["retcode"]}
            else:
                result = {"success": False, "reason": "qzone_result_unknown_no_retry"}
        except Exception:
            result = {"success": False, "reason": "qzone_result_unknown_no_retry"}
        self._save_receipt(event_key, state, result)
        return result
