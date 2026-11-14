"""Explicit chat commands -> persisted, bounded video subprocesses."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .platforms.common.tools import tool

ROOT = Path(__file__).resolve().parent.parent
PLATFORMS = r"B站|bilibili|哔哩哔哩|抖音|douyin"
NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class VideoCommand:
    action: str
    platform: str
    rounds: int = 5
    content: str = ""
    topics: tuple = ()
    post_comments: bool = True
    mode: str = 'share'


def video_command(text, source="wechat", chat_type="private"):
    if source in {"bilibili", "douyin"} and chat_type != "private":
        return None  # Public comment text cannot start autonomous browsing jobs.
    text = re.sub(r"^(?:@[^\s]+\s+)+", "", str(text)).strip()
    if re.fullmatch(r"(?:查看|查询|看看)?刷视频(?:进度|状态)|(?:视频)?刷完了吗[？?]?", text):
        return VideoCommand("status", source if source in {"bilibili", "douyin"} else "bilibili")
    fixed = text.startswith("[刷视频]")
    found = re.search(r"刷(?:(?:[0-9]{1,2}|[一二两三四五六七八九十])(?:轮|条|个))?(?:视频|B站视频|抖音视频)", text, re.I)
    if not fixed:
        if not found or not re.fullmatch(r"(?:(?:请|帮我|麻烦|现在|去|在|到|的|上)|\s|" + PLATFORMS + r")*", text[:found.start()], re.I):
            return None
        if re.search(r"怎么|如何|示例|模块|功能|实现|代码", text[found.end():]):
            return None
    names = re.findall(PLATFORMS, text, re.I)
    platforms = {"douyin" if name.lower() in {"抖音", "douyin"} else "bilibili" for name in names}
    if len(platforms) > 1:
        return None
    platform = next(iter(platforms), source if source in {"bilibili", "douyin"} else "bilibili")
    match = re.search(r"([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:轮|条|个视频)", text)
    rounds = (int(match[1]) if match[1].isascii() else NUMBERS[match[1]]) if match else 5
    if not 1 <= rounds <= 20:
        return None
    topics = ()
    if fixed:
        literal = re.search(r"(?:话题|兴趣|内容)[：:]([^\n]+)", text)
        if literal:
            topics = tuple(t.strip() for t in re.split("[,，、;；]", literal[1]) if t.strip())
    watch = bool(re.search(r'监视|监控|监听|跟进|守候|观察评论区|watch', text,re.I))
    if re.search(r'不(?:要)?(?:再)?(?:监视|监控|监听|跟进)|不看评论区',text):watch=False
    return VideoCommand("start", platform, rounds, text[:1500], topics,
                        not bool(re.search(r"只看|不(?:要)?(?:发)?评论", text)), 'watch' if watch else 'share')


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class VideoJobs:
    def __init__(self, directory, *, root=ROOT, launch=subprocess.Popen, alive=process_alive):
        self.directory = Path(directory) / "video-jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root, self.launch, self.alive = Path(root), launch, alive
        self.processes = {}
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / "jobs.sqlite3", timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,event_key TEXT UNIQUE,owner TEXT,platform TEXT,state TEXT,
            pid INTEGER,rounds INTEGER,created_at REAL)""")
        self.db.commit()

    def _refresh(self):
        for row in self.db.execute("SELECT * FROM jobs WHERE state IN ('starting','running','unknown')").fetchall():
            process = self.processes.get(row["id"])
            running = process.poll() is None if callable(getattr(process, "poll", None)) else self.alive(row["pid"])
            if running:
                continue
            report = self.directory / (row["id"] + ".result.json")
            state = "unknown"
            if report.is_file() and report.stat().st_size < 1_000_000:
                try:
                    data = json.loads(report.read_text(encoding="utf-8"))
                    state = ('unknown' if data.get('job_phase') in ('watching','running') else
                             "failed" if data.get("success") is False else "completed")
                except (ValueError, OSError):
                    pass
            elif row["state"] == "starting" and time.time() - row["created_at"] < 120:
                continue
            self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, row["id"]))
            cookie_file = self.directory / (row["id"] + ".cookies.json")
            cookie_file.unlink(missing_ok=True)
        self.db.commit()

    def start(self, command, *, owner, event_key, topics, cookies=(), config_path=None, share_target=None):
        if command.mode not in ('share','watch'):
            return dict(success=False, reason='video_mode_invalid')
        path = Path(config_path).resolve() if config_path else self.root / command.platform / "config.json"
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if config.get("enabled") is not True or config.get("video_social", {}).get("enabled") is not True:
            return dict(success=False, reason="video_platform_not_enabled", platform=command.platform)
        if (not isinstance(topics, (list, tuple)) or len(topics) > 20
                or any(not isinstance(t, str) or not t.strip() or len(t) > 100 for t in topics)):
            return dict(success=False, reason="video_topics_invalid")
        with self.lock:
            self._refresh()
            previous = self.db.execute("SELECT * FROM jobs WHERE event_key=?", (event_key,)).fetchone()
            if previous:
                return self._summary(previous, cached=True)
            self.db.execute("BEGIN IMMEDIATE")
            process = None
            try:
                running = self.db.execute("""SELECT * FROM jobs WHERE platform=?
                    AND state IN ('starting','running','unknown')""", (command.platform,)).fetchone()
                if running:
                    self.db.rollback()
                    return dict(success=False, reason="video_platform_busy_or_uncertain", platform=command.platform)
                job = uuid.uuid4().hex
                self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,'starting',0,?,?)",
                                (job, event_key, owner, command.platform, command.rounds, time.time()))
                self.db.commit()  # Persist the claim before starting any child process.
            except BaseException:
                self.db.rollback()
                raise
            try:
                config["base_config"] = str((path.parent / config["base_config"]).resolve())
                memory = Path(config["memory_dir"])
                config["memory_dir"] = str(memory if memory.is_absolute() else (path.parent / memory).resolve())
                if command.platform == "bilibili":
                    config["credentials_file"] = str((path.parent / config.get("credentials_file", "credentials.json")).resolve())
                elif config.get('transport') == 'api':
                    from .platforms.douyin.backend import backend_options
                    opts = backend_options(config, path)
                    config['api_backend'] = {**config.get('api_backend', {}), 'repo_dir': opts['repo'],
                        'python': opts['python'], 'credentials_file': opts['credentials']}
                policy = dict(config["video_social"])
                policy.update(max_videos_per_run=command.rounds, max_comments_per_run=command.rounds,
                              interaction_mode=command.mode,watch_after_seconds=7200 if command.mode=='watch' else 0)
                preset=dict(policy.get('comment_preset',{}))
                if share_target is not None:
                    if command.platform != 'douyin' or config.get('transport') != 'api':
                        raise ValueError('share_target_requires_douyin_api')
                    preset.update(owner_uid=share_target['uid'], owner_name=share_target['name'],
                        owner_sec_uid=str(share_target.get('sec_uid') or ''), address='朋友',
                        instructions='向发指令的好友分享本视频的可验证内容与一个具体画面，不泄露私信内容。')
                if topics:
                    policy.update(selection_mode='search_order', search_fallback=False, interests=list(topics))
                elif share_target is not None:
                    policy.update(selection_mode='recommendation_order', search_fallback=False)
                preset['enabled']=command.mode=='share' and command.post_comments
                policy['comment_preset']=preset
                if command.mode=='share' and command.post_comments and (not preset.get('owner_uid') or not preset.get('owner_name')):
                    raise ValueError('owner_share_target_missing')
                policy["post_comments"] = policy.get("post_comments", True) and command.post_comments
                if topics:
                    policy["interests"] = list(topics)
                config["video_social"] = policy
                # The child never duplicates the standing private-chat listener.
                config['peers']=[]; config['private_chat_mode']='allowlist'; config['all_peers']=False
                config['comments_enabled']=False
                config['comment_replies_enabled']=command.mode=='watch' and command.post_comments
                config_path = self.directory / (job + ".config.json")
                config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                args = [sys.executable, str(self.root / "run_video_social.py"), "run", command.platform,
                        "--config", str(config_path), "--control-config", str(path),
                        "--count", str(command.rounds),
                        "--output", str(self.directory / (job + ".result.json"))]
                if command.platform == "douyin" and cookies and config.get('transport') != 'api':
                    cookie_path = self.directory / (job + ".cookies.json")
                    cookie_path.write_text(json.dumps(list(cookies)), encoding="utf-8")
                    args.extend(["--cookies-file", str(cookie_path)])
                with (self.directory / (job + ".log")).open("wb") as log:
                    process = self.launch(args, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        **({"start_new_session": True} if os.name != "nt" else {}))
                self.processes[job] = process
                with self.db:
                    self.db.execute("UPDATE jobs SET state='running',pid=? WHERE id=?", (process.pid, job))
            except Exception:
                with self.db:
                    self.db.execute("UPDATE jobs SET state=? WHERE id=?", ("unknown" if process is not None else "failed", job))
                if process is None:
                    (self.directory / (job + ".cookies.json")).unlink(missing_ok=True)
                return dict(success=False, reason="video_job_start_failed")
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            return self._summary(row)

    def _summary(self, row, *, cached=False):
        state = row["state"]
        result = dict(success=state in {"starting", "running", "completed"}, job_id=row["id"],
                      platform=row["platform"], state=state, requested_rounds=row["rounds"],
                      cached=cached, background=True, message_sent=False,
                      side_effect_performed=state in {"starting", "running", "completed", "unknown"})
        path = self.directory / (row["id"] + ".result.json")
        if path.is_file() and path.stat().st_size < 1_000_000:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.update(opened=data.get("opened", 0), published=data.get("published", 0),
                              pending_visibility=data.get("pending_visibility", 0), reason=data.get("reason", ""))
                result.update(attempted=data.get('attempted', data.get('opened', 0)),
                    requested=data.get('requested', row['rounds']), skipped=len(data.get('skipped', [])),
                    recommendation_refresh=data.get('recommendation_refresh'))
                result.update(mode=data.get('mode'), comment_watch=data.get('comment_watch'))
                if data.get('success') is False:
                    result['success'] = False
                result["comment_failures"] = sum(item.get("publication") in {"failed_before_send", "unknown_no_retry"}
                    for item in data.get("records", []))
                result["uncertain_publications"] = sum(item.get("publication") == "unknown_no_retry"
                    for item in data.get("records", []))
                result["verified_analyses"] = sum(item.get("analysis_verified") is True for item in data.get("records", []))
                result["understandings"] = sum(bool(item.get("understanding")) for item in data.get("records", []))
                result["unresolved_analyses"] = sum(item.get("analysis_verified") is False for item in data.get("records", []))
                if state == "completed" and not data.get("opened"):
                    result["success"] = False
            except (ValueError, OSError):
                pass
        return result

    def status(self, owner):
        with self.lock:
            self._refresh()
            row = self.db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT 1", (owner,)).fetchone()
            return self._summary(row) if row else dict(success=True, state="no_job")

    def close(self):
        with self.lock:
            self.db.close()


class VideoChatTools:
    def __init__(self, delegate, service, message, command):
        self.delegate, self.service, self.message, self.command = delegate, service, message, command

    def __getattr__(self, name):
        if self.delegate is None:
            raise AttributeError(name)
        return getattr(self.delegate, name)

    @property
    def preferred_tool(self):
        return ('video_browse_watch' if self.command.mode=='watch' else 'video_browse_share') if self.command.action=='start' else 'video_browse_status'

    @property
    def definitions(self):
        if self.command.action == "status":
            current = tool("video_browse_status", "查看当前聊天发起的最近一次刷视频任务状态。", {}, [])
        else:
            current = tool(self.preferred_tool,
                f"当前消息明确要求刷视频。启动{self.command.platform}后台任务，最多{self.command.rounds}条后退出。"
                + ('每条@主人分享后切换下一条，不读取或监视评论回复。' if self.command.mode=='share' else '不@主人；全部视频处理后，仅跟进本次评论楼回复两小时。') +
                "根据本条用户内容提炼兴趣topics；平台和轮数由程序绑定。confirm=true表示本条明确指令。",
                {"topics": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 20},
                 "confirm": {"type": "boolean"}}, ["topics", "confirm"])
        return [*getattr(self.delegate, "definitions", ()), current]

    def execute(self, name, arguments):
        if name not in {"video_browse_share", "video_browse_watch", "video_browse_status"}:
            return self.delegate.execute(name, arguments) if self.delegate else dict(success=False, reason="tool_unavailable")
        if name != self.preferred_tool or not self.service.settings.runtime.tool_calling_enabled:
            return dict(success=False, reason="explicit_video_command_required")
        if name != 'video_browse_status' and arguments.get("confirm") is not True:
            return dict(success=False, reason="explicit_video_command_required")
        service = self.service
        # Initialization is also used from WeChat worker threads.
        with service._video_jobs_lock:
            if getattr(service, "_video_jobs", None) is None:
                service._video_jobs = VideoJobs(Path(service.settings.runtime.database_path).parent)
            jobs = service._video_jobs
        scope = f"{self.message.platform}:{service.settings.runtime.memory_account_id}:{self.message.chat_type}:{self.message.chat_id or self.message.sender_id}"
        owner = hashlib.sha256(scope.encode()).hexdigest()
        if name == "video_browse_status":
            return jobs.status(owner)
        identity = self.message.message_id or f"{self.message.text}:{int(time.time()//60)}"
        event_key = hashlib.sha256((scope + ":" + identity).encode()).hexdigest()
        topics = self.command.topics or arguments.get("topics", [])
        provider = getattr(service, "video_session_provider", None)
        cookies = provider() if self.command.platform == "douyin" and callable(provider) else ()
        return jobs.start(self.command, owner=owner, event_key=event_key, topics=topics, cookies=cookies)
