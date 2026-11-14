"""B站私信和@评论通知：固定网站接口、目标绑定、写请求不重试。"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import replace
from urllib.parse import urlencode, urlsplit
from urllib.request import Request
from urllib.error import HTTPError, URLError

from .client import BilibiliClient, BilibiliError, wbi_sign
from ..common.chat import SocialEvent


def positive(value):
    text = str(value)
    return text if re.fullmatch(r"[1-9]\d*", text) else ""


def event_time(value):
    try:
        stamp = float(value)
        if stamp > 1e14:
            stamp /= 1e6
        elif stamp > 1e11:
            stamp /= 1000
        return stamp if 0 < stamp <= time.time() + 300 else 0
    except (TypeError, ValueError):
        return 0


def video_links(text):
    from ...online_video import platform_url
    media = []
    for url in re.findall(r"https?://[^\s<>]+", text):
        try:
            platform, url = platform_url(url.rstrip("，。！？,.)"))
            if platform == "bilibili":
                media.append({"kind": "video", "url": url})
        except (ValueError, RuntimeError):
            continue
    return tuple(media[:3])


class BilibiliSocialClient(BilibiliClient):
    def __init__(self, sessdata="", csrf="", **kwargs):
        super().__init__(sessdata, **kwargs)
        if not isinstance(csrf, str) or any(c in csrf for c in "\r\n;"):
            raise ValueError("bili_jct 只填写 Cookie 字段值")
        self.csrf = csrf
        self._wbi = None

    def set_browser_cookies(self, cookies):
        """Reuse cookies obtained by this account's normal dedicated browser."""
        allowed = {"buvid3", "buvid4", "DedeUserID", "b_nut", "bili_jct"}
        for cookie in cookies:
            if cookie.get("domain", "").lstrip(".") not in {"bilibili.com", "www.bilibili.com"}:
                continue
            if cookie.get("name") == "SESSDATA" and cookie.get("value") != self.sessdata:
                raise BilibiliError("ACCOUNT_MISMATCH")
            key, value = cookie.get("name"), cookie.get("value")
            if key in allowed and isinstance(value, str) and not any(c in value for c in "\r\n;"):
                self.cookies[key] = value

    def _signed(self, params):
        if not self._wbi or time.monotonic() - self._wbi[0] > 180:
            keys = self.api("/x/web-interface/nav").get("wbi_img", {})
            img = urlsplit(keys.get("img_url", "")).path.rsplit("/", 1)[-1].split(".")[0]
            sub = urlsplit(keys.get("sub_url", "")).path.rsplit("/", 1)[-1].split(".")[0]
            self._wbi = (time.monotonic(), img, sub)
        return wbi_sign(params, self._wbi[1], self._wbi[2], int(time.time()))

    def _social(self, host, path, params=None, *, data=None, signed=False):
        if host not in {"api.bilibili.com", "api.vc.bilibili.com"} or not path.startswith("/"):
            raise BilibiliError("UNSAFE_URL")
        if not self.sessdata or (data is not None and not self.csrf):
            raise BilibiliError("LOGIN_AND_CSRF_REQUIRED")
        params = self._signed(params or {}) if signed else (params or {})
        parts = {**self.cookies, "SESSDATA": self.sessdata}
        if self.csrf:
            parts["bili_jct"] = self.csrf
        referer = "https://www.bilibili.com/" if path.startswith("/x/v2/reply/") else "https://message.bilibili.com/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36", "Referer": referer,
                   "Cookie": "; ".join(f"{key}={value}" for key, value in parts.items())}
        if data is not None:
            data = urlencode({**data, "csrf": self.csrf, "csrf_token": self.csrf}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://www.bilibili.com" if host == "api.bilibili.com" else "https://message.bilibili.com"
        request = Request("https://" + host + path + "?" + urlencode(params), data=data, headers=headers)
        try:
            # 原始 opener 禁止跳转；发送只调用一次，不使用带网络重试的 net.urlopen。
            with self._open(request) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise BilibiliError("RESPONSE_TOO_LARGE")
            envelope = json.loads(raw)
            if envelope.get("code") != 0:
                error = BilibiliError({-101: "LOGIN_REQUIRED", -412: "RISK_CONTROL", -403: "FORBIDDEN"}.get(envelope.get("code"), "API_REJECTED"))
                error.api_code = envelope.get("code") if type(envelope.get("code")) is int else None
                raise error
            if not isinstance(envelope.get("data"), dict):
                raise BilibiliError("BAD_RESPONSE")
            return envelope["data"]
        except HTTPError as exc:
            raise BilibiliError(f"HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise BilibiliError("NETWORK_ERROR") from None
        except (ValueError, AttributeError):
            raise BilibiliError("BAD_RESPONSE") from None

    def messages(self, peer):
        if not positive(peer):
            raise ValueError("私信 UID 无效")
        data = self._social("api.vc.bilibili.com", "/svr_sync/v1/svr_sync/fetch_session_msgs",
                            {"talker_id": peer, "session_type": 1, "size": 30, "begin_seqno": 0}, signed=True)
        rows = data.get("messages") or []
        if not isinstance(rows, list):
            raise BilibiliError("BAD_RESPONSE")
        return rows

    def mentions(self):
        data = self._social("api.bilibili.com", "/x/msgfeed/at", {"platform": "web", "build": 0, "mobi_app": "web"})
        rows = data.get("items") or []
        if not isinstance(rows, list):
            raise BilibiliError("BAD_RESPONSE")
        return rows[:100]

    def sessions(self, *, max_pages=100):
        """Read all personal chat sessions; refuse truncated or looping pagination."""
        peers, seen, cursor = [], set(), None
        for _ in range(max_pages):
            params = dict(session_type=1, group_fold=1, unfollow_fold=0, sort_rule=2, build=0, mobi_app="web")
            if cursor is not None:
                params["begin_ts"] = cursor
            data = self._social("api.vc.bilibili.com", "/session_svr/v1/session_svr/get_sessions", params)
            rows = data.get("session_list") or []
            if not isinstance(rows, list):
                raise BilibiliError("BAD_SESSION_RESPONSE")
            for row in rows:
                if not isinstance(row, dict):
                    raise BilibiliError("BAD_SESSION_RESPONSE")
                peer = positive(row.get("talker_id"))
                if row.get("session_type") == 1 and peer and peer not in peers:
                    peers.append(peer)
            if not data.get("has_more"):
                return peers
            stamps = [int(row["session_ts"]) for row in rows if positive(row.get("session_ts"))]
            following = min(stamps) if stamps else None
            if following is None or following in seen or (cursor is not None and following >= cursor):
                raise BilibiliError("SESSION_PAGINATION_UNCONFIRMED")
            seen.add(following)
            cursor = following
        raise BilibiliError("SESSION_PAGE_LIMIT")

    def replies(self):
        data = self._social("api.bilibili.com", "/x/msgfeed/reply", dict(platform="web", build=0, mobi_app="web"))
        rows = data.get("items") or []
        if not isinstance(rows, list):
            raise BilibiliError("BAD_RESPONSE")
        return rows[:100]

    def notification_hints(self):
        data = self._social("api.bilibili.com", "/x/msgfeed/unread", dict(build=0, mobi_app="web"))
        return {key: max(0, int(data.get(key, 0))) for key in ("at", "reply")}

    def post_video_comment(self, url, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise BilibiliError("INVALID_COMMENT_TEXT")
        try:
            detail = self.video(url)
        except BilibiliError as exc:
            exc.before_write = True
            raise
        oid = positive(detail.get("aid"))
        if not oid:
            raise BilibiliError("INVALID_COMMENT_VIDEO")
        result = self._social("api.bilibili.com", "/x/v2/reply/add",
                             data=dict(oid=oid, type=1, root=0, parent=0, message=text, plat=1))
        root = positive(result.get("rpid") or (result.get("reply") or {}).get("rpid"))
        if not root:
            raise BilibiliError("SEND_RESULT_UNCONFIRMED")
        return dict(root_id=root, video_id=oid, type=1)

    def send_private(self, account, peer, text):
        if not positive(account) or not positive(peer) or account == peer or not text.strip() or len(text) > 1000:
            raise BilibiliError("INVALID_PRIVATE_TARGET_OR_TEXT")
        result = self._social("api.vc.bilibili.com", "/web_im/v1/web_im/send_msg",
            {"w_sender_uid": account, "w_receiver_id": peer}, signed=True,
            data={"msg[sender_uid]": account, "msg[receiver_id]": peer, "msg[receiver_type]": 1,
                  "msg[msg_type]": 1, "msg[msg_status]": 0, "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
                  "msg[dev_id]": str(uuid.uuid4()).upper(), "msg[new_face_version]": 0,
                  "msg[timestamp]": int(time.time()), "build": 0, "mobi_app": "web"})
        if not positive(result.get("msg_key")):
            raise BilibiliError("SEND_RESULT_UNCONFIRMED")
        return dict(msg_key=str(result["msg_key"]), receiver_id=peer, sender_uid=account)

    def reply_comment(self, target, text):
        if not self.sessdata or not self.csrf:
            raise BilibiliError("LOGIN_AND_CSRF_REQUIRED")
        if any(not positive(target.get(key)) for key in ("oid", "parent", "root")) or target.get("type") not in (1, 11, 12, 17):
            raise BilibiliError("INVALID_COMMENT_TARGET")
        if not text.strip() or len(text) > 800:
            raise BilibiliError("INVALID_COMMENT_TEXT")
        # 通知中的评论已经删除、路由变化或接口无法确认目标时，不另发顶层评论。
        detail = self.api("/x/v2/reply/jump", {"oid": target["oid"], "type": target["type"], "rpid": target["parent"]})
        def find(rows):
            for row in rows:
                if isinstance(row, dict):
                    if str(row.get("rpid")) == target["parent"]:
                        return row
                    child = find(row.get("replies") or [])
                    if child:
                        return child
            return None
        row = find([detail.get("root"), *(detail.get("replies") or [])])
        if row is None or str(row.get("root") or row.get("rpid")) != target["root"]:
            raise BilibiliError("COMMENT_TARGET_UNCONFIRMED")
        result = self._social("api.bilibili.com", "/x/v2/reply/add", data={**target, "message": text, "plat": 1})
        if not positive(result.get("rpid") or (result.get("reply") or {}).get("rpid")):
            raise BilibiliError("SEND_RESULT_UNCONFIRMED")


def private_event(row, account, peer):
    try:
        sender = positive(row.get("sender_uid"))
        mid = positive(row.get("msg_key")) or positive(row.get("msg_seqno"))
        if sender != str(peer) or str(row.get("receiver_id")) != account or row.get("receiver_type") != 1 or not mid:
            return None
        body = json.loads(row["content"]) if isinstance(row.get("content"), str) else row.get("content", {})
        kind = row.get("msg_type")
        if kind == 1:
            text, media = str(body.get("content") or ""), ()
            media = video_links(text)
        elif kind == 2:
            text, media = "[图片消息]", ({"kind": "image", "url": str(body.get("url") or "")},)
        elif kind == 7 and re.fullmatch(r"BV[A-Za-z0-9]{10}", str(body.get("bvid", ""))):
            text = "[分享的视频，请读取实际内容后回复]"
            media = ({"kind": "video", "url": "https://www.bilibili.com/video/" + body["bvid"]},)
        else:
            return None
        return SocialEvent("bilibili", account, "private", str(peer) + ":" + mid, sender, text, str(peer), media,
                           created_at=event_time(row.get("timestamp")))
    except (ValueError, TypeError, AttributeError):
        return None


def mention_event(row, account):
    try:
        item, user = row["item"], row["user"]
        sender, mid = positive(user.get("mid")), positive(row.get("id"))
        oid, parent = positive(item.get("subject_id")), positive(item.get("source_id"))
        root = positive(item.get("root_id")) or parent
        business = int(item.get("business_id", 0))
        if (not all((sender, mid, oid, parent, root)) or sender == account or business not in (1, 11, 12, 17)
                or item.get("type") not in ("reply", "comment") or item.get("hide_reply_button") is True):
            return None
        text = str(item.get("source_content") or "").strip()
        if not text:
            return None
        videos = video_links(str(item.get("uri") or ""))
        source_video = ""
        if videos:
            parsed = urlsplit(videos[0]["url"])
            if re.fullmatch(r"/video/BV[A-Za-z0-9]{10}/?", parsed.path):
                source_video = "https://www.bilibili.com" + parsed.path
        # @ and reply notifications for the same source comment share a delivery key.
        return SocialEvent("bilibili", account, "comment", f"comment:{business}:{oid}:{parent}", sender, text,
                           f"{account}:comment:{business}:{oid}:{root}", mentioned=True,
                           target={"oid": oid, "type": business, "root": root, "parent": parent}, sender_name=str(user.get("nickname") or ""),
                           source_video=source_video, created_at=event_time(row.get("at_time") or row.get("reply_time")))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class BilibiliSource:
    def __init__(self, api, account, registry=None):
        self.api, self.account, self.registry = api, account, registry

    def poll(self, stream):
        if stream in ("mentions", "replies"):
            rows = self.api.mentions() if stream == "mentions" else self.api.replies()
            events = [mention_event(row, self.account) for row in reversed(rows)]
            if stream == "replies":
                verified = []
                for event in events:
                    if event and self.registry and self.registry.owns("bilibili", self.account,
                            event.target["root"], video_id=event.target["oid"]):
                        thread = self.registry.get("bilibili", self.account, event.target["root"])
                        verified.append(replace(event, mentioned=False, reply_to_own=True,
                                                source_video=thread["video_url"]))
                events = verified
        else:
            rows = self.api.messages(stream)
            rows = sorted((row for row in rows if isinstance(row, dict)),
                          key=lambda row: int(positive(row.get("msg_seqno")) or 0))
            events = [private_event(row, self.account, stream) for row in rows]
        return [event for event in events if event is not None]

    def send(self, event, text):
        if event.channel == "private":
            self.api.send_private(self.account, event.sender, text)
        else:
            self.api.reply_comment(event.target, text)
