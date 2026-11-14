"""Bilibili recommendation participation through account-bound website APIs."""
from __future__ import annotations

import time

from .chat import BilibiliChatPump
from .client import BilibiliError


class BilibiliVideoInteraction:
    def __init__(self, api, account, registry, *, service=None, config=None, reader=None,
                 permitted=lambda: True, dry_run=False, cookie_provider=None, browser_comments=None, transport="api"):
        self.api, self.account, self.registry = api, str(account), registry
        self.permitted, self.dry_run = permitted, dry_run
        self.cookie_provider = cookie_provider
        self.browser_comments, self.transport = browser_comments, transport
        preset=(config or {}).get('video_social',{}).get('comment_preset',{})
        self.mention_target=(dict(uid=preset['owner_uid'],name=preset['owner_name']) if preset.get('enabled') else None)
        self.pump = (BilibiliChatPump(api, service, config, self.account, registry,
                                     reader=reader, permitted=permitted) if service and not dry_run else None)
        self.next_poll, self.hints = 0, {}

    def verify_account(self):
        if callable(self.cookie_provider):
            self.api.set_browser_cookies(self.cookie_provider())
        current = self.api.account()
        if not current.get("logged_in") or current.get("uid") != self.account:
            raise BilibiliError("ACCOUNT_MISMATCH")

    def publish(self, url, text):
        if self.dry_run or not self.permitted():
            raise BilibiliError("VIDEO_COMMENT_DISABLED")
        if self.mention_target:
            try:
                self.verify_account()
                card=self.api.api('/x/web-interface/card',{'mid':self.mention_target['uid']}).get('card',{})
                if str(card.get('mid'))!=self.mention_target['uid'] or card.get('name')!=self.mention_target['name']:
                    raise BilibiliError('MENTION_TARGET_CHANGED')
                if not text.startswith('@'+self.mention_target['name']+' '):
                    raise BilibiliError('MENTION_TARGET_NOT_BOUND')
            except BilibiliError as exc:
                exc.before_write=True
                raise
        if self.browser_comments is not None and self.transport != "api":
            try:
                return self.browser_comments.publish(url, text)
            except Exception as exc:
                if not (self.transport == "auto" and getattr(exc, "before_write", False)
                        and getattr(exc, "fallback_safe", False)):
                    raise
        try:
            self.verify_account()
        except BilibiliError as exc:
            exc.before_write = True
            raise
        return self.api.post_video_comment(url, text)

    def poll(self):
        if time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 20
        try:
            self.verify_account()
        except BilibiliError as exc:
            if exc.code in {"ACCOUNT_MISMATCH", "LOGIN_REQUIRED", "RISK_CONTROL", "FORBIDDEN"}:
                raise
            self.hints = {"account_read_temporarily_unavailable": True}
            self.next_poll = time.monotonic() + 60
            return
        try:
            self.hints = self.api.notification_hints()
        except BilibiliError:
            self.hints = {"unread_hint_available": False}
        # Hints are advisory: a stale badge must not suppress a real reply.
        if self.pump:
            self.pump.step()
