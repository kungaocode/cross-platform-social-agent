"""B站聊天/评论监听启动器；旧视频 ask/chat 入口保持独立。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ...service import BotService
from ..common.chat import SocialResponder, PollLoop, platform_settings
from ..common.media import SocialMediaReader
from .social import BilibiliSocialClient, BilibiliSource, positive
from .client import BilibiliError


def validate_config(config):
    for name in ("enabled", "comments_enabled", "comment_replies_enabled"):
        if not isinstance(config.get(name, False), bool):
            raise ValueError(name + " 必须是布尔值")
    for name in ("peers", "comment_senders"):
        values = config.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not positive(value) for value in values):
            raise ValueError(name + " 必须为 UID 字符串数组")
    if config.get("account_uid") and (not isinstance(config["account_uid"], str) or not positive(config["account_uid"])):
        raise ValueError("account_uid 必须为自己的 UID 字符串")
    if not 20 <= int(config.get("poll_seconds", 20)) <= 60:
        raise ValueError("poll_seconds 须为 20～60")
    if config.get("private_chat_mode", "allowlist") not in ("allowlist", "all_sessions"):
        raise ValueError("private_chat_mode 须为 allowlist 或 all_sessions")
    if not 1 <= int(config.get("private_batch_size", 20)) <= 100:
        raise ValueError("private_batch_size 须为 1～100")


class BilibiliChatPump:
    """Refresh the complete chat list and poll it fairly; new chats need no UID edits."""
    def __init__(self, api, service, config, identity, registry, *, reader=None, permitted=lambda: True, baseline_only=False):
        self.api, self.config, self.identity = api, config, identity
        self.source = BilibiliSource(api, identity, registry)
        self.responder = SocialResponder(service, "bilibili", identity, config=config,
                                         reader=reader, permitted=permitted)
        self.read_errors, self.retry_after = {}, {}
        self.loop = PollLoop(self.responder, self.poll, self.source.send,
                            max_replies=config.get("max_replies_per_poll", 5),
                            baseline_before=None if baseline_only else time.time())
        self.peers, self.next_refresh, self.cursor = [], 0, 0

    def poll(self, stream):
        if time.monotonic() < self.retry_after.get(stream, 0):
            return None
        try:
            events = self.source.poll(stream)
            self.read_errors.pop(stream, None)
            return events
        except BilibiliError as exc:
            if exc.code in {"LOGIN_REQUIRED", "RISK_CONTROL", "ACCOUNT_MISMATCH"}:
                raise
            self.read_errors[stream] = exc.code
            self.retry_after[stream] = time.monotonic() + 60
            return None

    def refresh(self):
        if time.monotonic() >= self.next_refresh:
            try:
                peers = (self.api.sessions() if self.config.get("private_chat_mode") == "all_sessions"
                         else self.config.get("peers", []))
                self.read_errors.pop("session_list", None)
            except BilibiliError as exc:
                if exc.code in {"LOGIN_REQUIRED", "RISK_CONTROL", "ACCOUNT_MISMATCH"}:
                    raise
                self.read_errors["session_list"] = exc.code
                peers = []
            self.peers = [peer for peer in peers if peer != self.identity]
            self.responder.set_private_peers(self.peers)
            self.next_refresh = time.monotonic() + 30
        return self.peers

    def step(self):
        self.refresh()
        count = min(len(self.peers), int(self.config.get("private_batch_size", 20)))
        peers = [self.peers[(self.cursor + i) % len(self.peers)] for i in range(count)]
        self.cursor = (self.cursor + count) % max(1, len(self.peers))
        notices = (["mentions"] if self.config.get("comments_enabled") else [])
        notices += (["replies"] if self.config.get("comment_replies_enabled") else [])
        self.loop.step([*notices, *peers])


def run_social(config_path, settings, memory, token, csrf, *, once=False, inbox=False):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    validate_config(config)
    if not inbox and (config.get("enabled") is not True or not (
            config.get("peers") or config.get("private_chat_mode") == "all_sessions"
            or config.get("comments_enabled") or config.get("comment_replies_enabled"))):
        raise ValueError("请配置 B 站 peers 或 comments_enabled，再开启 enabled")
    if not token or (not inbox and not csrf):
        raise BilibiliError("LISTEN_REQUIRES_SESSDATA_AND_BILI_JCT")
    api = BilibiliSocialClient(token, csrf)
    account = api.account()
    identity = positive(account.get("uid"))
    if not account["logged_in"] or not identity:
        raise BilibiliError("LOGIN_REQUIRED")
    if config.get("account_uid") and config["account_uid"] != identity:
        raise BilibiliError("ACCOUNT_MISMATCH")
    from ...video_participation import ThreadRegistry
    registry = ThreadRegistry(Path(memory) / "video-social.sqlite3")
    if inbox:
        try:
            source = BilibiliSource(api, identity, registry)
            peers = api.sessions() if config.get("private_chat_mode") == "all_sessions" else config.get("peers", [])
            streams = [*peers, *(["mentions"] if config.get('comments_enabled') else []),
                       *(["replies"] if config.get("comment_replies_enabled") else [])]
            print(json.dumps([{ "channel": event.channel, "id": event.id, "sender": event.sender,
                                "text": event.text, "media_count": len(event.media), "thread": event.thread }
                              for stream in streams for event in source.poll(stream)], ensure_ascii=False, indent=2))
        finally:
            registry.close()
        return

    def permitted():
        current = json.loads(config_path.read_text(encoding="utf-8-sig"))
        return current == config and current.get("enabled") is True

    settings = platform_settings(settings, "bilibili", {**config,'account_uid':identity}, Path(memory) / f"bilibili-{identity}.sqlite3")
    service = BotService(settings)
    reader = SocialMediaReader(settings, "bilibili", image_enabled=config.get("image_reading_enabled", True),
                               video_enabled=config.get("video_reading_enabled", True),
                               cookies=lambda: [{"name": "SESSDATA", "value": token, "domain": ".bilibili.com", "path": "/", "secure": True}])
    pump = BilibiliChatPump(api, service, config, identity, registry, reader=reader, permitted=permitted, baseline_only=once)
    print("B站监听已启动；历史消息建立基线，回复范围按 private_chat_mode、@通知及本人评论楼配置执行。Ctrl+C 停止。")
    announced_ready=False
    try:
        while permitted():
            if api.account().get("uid") != identity:
                raise BilibiliError("ACCOUNT_MISMATCH")
            pump.step()
            if not announced_ready:
                print(json.dumps(dict(event='bilibili_listener_ready',private_session_count=len(pump.peers),
                    initialized_streams=len(pump.loop.initialized),read_error_count=len(pump.read_errors),
                    image_reading_enabled=config.get('image_reading_enabled',True)),ensure_ascii=False),flush=True)
                announced_ready=True
            if once:
                print(json.dumps(dict(baseline_only=True, private_session_count=len(pump.peers),
                    initialized_streams=len(pump.loop.initialized), read_error_count=len(pump.read_errors),
                    social_messages_sent=0), ensure_ascii=False))
                if pump.read_errors:
                    raise BilibiliError("PARTIAL_READ_FAILURE")
                break
            time.sleep(int(config.get("poll_seconds", 20)))
    finally:
        service.close()
        registry.close()
