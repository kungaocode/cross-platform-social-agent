"""Account-bound, isolated HTTP/WebSocket transport for the optional Spider backend."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
PIN = 'd151f9c2980abc304c1ae4918c2272675cd9e4c2'


class DouyinAPIError(RuntimeError):
    def __init__(self, code, *, before_write=False, diagnostics=None):
        self.code = code if re.fullmatch(r'DOUYIN_[A-Z0-9_]+', str(code)) else 'DOUYIN_BACKEND_FAILED'
        self.before_write = before_write
        self.diagnostics = diagnostics or {}
        super().__init__(self.code)


def positive(value):
    value = str(value or '')
    return value if re.fullmatch(r'[1-9][0-9]{0,24}', value) else ''


def video_id(url):
    match = re.fullmatch(r'https://www\.douyin\.com/video/([1-9][0-9]{0,24})', str(url))
    if not match:
        raise DouyinAPIError('DOUYIN_INVALID_VIDEO_URL', before_write=True)
    return match[1]


def backend_options(config, config_path):
    raw = config.get('api_backend', {})
    if not isinstance(raw, dict) or config.get('transport', 'browser') not in ('browser', 'api'):
        raise ValueError('抖音 transport 须为 browser 或 api，api_backend 须为对象')
    base = Path(config_path).resolve().parent
    repo = Path(raw.get('repo_dir', ROOT / 'third_party/DouYin_Spider'))
    repo = repo if repo.is_absolute() else base / repo
    python = Path(raw.get('python', repo / '.venv/Scripts/python.exe'))
    python = python if python.is_absolute() else base / python
    credentials = Path(raw.get('credentials_file', Path(config['memory_dir']) / ('douyin-api-'+config['account_id']+'.json')))
    credentials = credentials if credentials.is_absolute() else base / credentials
    uid = str(raw.get('expected_uid', ''))
    if uid and not positive(uid):
        raise ValueError('api_backend.expected_uid 须为数字 UID')
    if type(raw.get('writes_enabled', False)) is not bool:
        raise ValueError('api_backend.writes_enabled 须为布尔值')
    limit = raw.get('max_source_seconds', 300)
    if limit is None:
        limit = 0
    if type(limit) is not int or not (limit == 0 or 2 <= limit <= 7200):
        raise ValueError('api_backend.max_source_seconds 须为 0（不限时长）或 2～7200 的整数')
    return dict(repo=str(repo.resolve()), python=str(python.resolve()), credentials=str(credentials.resolve()),
                expected_uid=uid, writes_enabled=raw.get('writes_enabled', False), max_source_seconds=limit)


def read_credentials(path):
    path = Path(path)
    if not path.is_file():
        raise DouyinAPIError('DOUYIN_CREDENTIALS_MISSING')
    if path.stat().st_size > 500000:
        raise DouyinAPIError('DOUYIN_CREDENTIALS_INVALID')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise DouyinAPIError('DOUYIN_CREDENTIALS_INVALID') from None
    if (not isinstance(value, dict) or not isinstance(value.get('cookie'), str)
            or not re.search(r'(?:^|;\s*)sessionid=[^;\s]+', value['cookie'])):
        raise DouyinAPIError('DOUYIN_CREDENTIALS_INVALID')
    return value


class SpiderClient:
    def __init__(self, options, *, permitted=lambda: True, discover=False):
        self.options, self.permitted = options, permitted
        self.process = None
        self.uid = ''
        self.lock = threading.Lock()
        self.responses = queue.Queue(maxsize=4)
        self.credential_stamp = b''
        self.discover = discover

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def start(self):
        if not Path(self.options['python']).is_file():
            raise DouyinAPIError('DOUYIN_BACKEND_PYTHON_MISSING')
        # A dead worker leaves an EOF sentinel in the old queue; never let it poison the new one.
        self.responses = queue.Queue(maxsize=4)
        credentials = read_credentials(self.options['credentials'])
        self.credential_stamp = Path(self.options['credentials']).read_bytes()
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        # The upstream loader must not silently mix another account's env credentials.
        for key in tuple(env):
            if key.startswith('DY_'):
                env.pop(key)
        self.process = subprocess.Popen([self.options['python'], '-u', str(Path(__file__).with_name('backend_worker.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', env=env, cwd=ROOT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        process = self.process
        def read():
            for line in process.stdout:
                try:
                    self.responses.put_nowait(line if len(line) < 4_000_000 else '')
                except queue.Full:
                    return
            try:
                self.responses.put_nowait('')
            except queue.Full:
                pass

        threading.Thread(target=read, daemon=True).start()
        try:
            self.call('init', dict(repo=self.options['repo'], credentials=credentials,
                                  writes_enabled=self.options['writes_enabled']))
            account = self.call('account')
            self.uid = positive(account.get('uid'))
            expected = self.options['expected_uid'] or str(credentials.get('uid', ''))
            if not self.uid or (expected and self.uid != expected) or (not expected and not self.discover):
                raise DouyinAPIError('DOUYIN_ACCOUNT_MISMATCH')
        except BaseException:
            self.close()
            raise

    def call(self, method, args=None):
        with self.lock:
            if not self.permitted():
                raise DouyinAPIError('DOUYIN_DISABLED', before_write=True)
            if self.credential_stamp and Path(self.options['credentials']).read_bytes() != self.credential_stamp:
                raise DouyinAPIError('DOUYIN_CREDENTIALS_CHANGED', before_write=True)
            if self.process is None or self.process.poll() is not None:
                raise DouyinAPIError('DOUYIN_BACKEND_STOPPED', before_write=True)
            try:
                self.process.stdin.write(json.dumps(dict(method=method, args=args or {}), ensure_ascii=False)+'\n')
                self.process.stdin.flush()
                response = self.responses.get(timeout=60)
                result = json.loads(response)
            except Exception:
                self.close()
                raise DouyinAPIError('DOUYIN_BACKEND_TIMEOUT_OR_STOPPED') from None
            if result.get('ok') is not True:
                code = result.get('code', 'DOUYIN_BACKEND_FAILED')
                raise DouyinAPIError(code, diagnostics=result.get('diagnostics'), before_write=result.get('before_write') is True or code in {
                    'DOUYIN_API_WRITES_DISABLED', 'DOUYIN_WRITE_CREDENTIALS_MISSING',
                    'DOUYIN_WRITE_SESSION_MISMATCH', 'DOUYIN_PRIVATE_TEXT_INVALID',
                    'DOUYIN_MENTION_TARGET_MISSING', 'DOUYIN_MENTION_TARGET_MISMATCH',
                    'DOUYIN_MENTION_TEXT_MISMATCH',
                    'DOUYIN_CONVERSATION_UNCONFIRMED'})
            return result['data']

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout:
            process.stdout.close()

    def feed(self, index=2):
        return self.call('feed', {'index': index})['aweme_list']

    def detail(self, url):
        vid = video_id(url)
        row = self.call('detail', {'url': url})['aweme_detail']
        if row is None:
            raise DouyinAPIError('DOUYIN_VIDEO_UNAVAILABLE')
        if not isinstance(row, dict) or str(row.get('aweme_id')) != vid:
            raise DouyinAPIError('DOUYIN_VIDEO_MISMATCH')
        return row

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 100:
            raise DouyinAPIError('DOUYIN_INVALID_QUERY')
        rows = self.call('search', {'query': query})['data']
        return [r['aweme_info'] for r in rows if isinstance(r, dict) and isinstance(r.get('aweme_info'), dict)]

    def comments(self, url):
        video_id(url)
        return self.call('comments', {'url': url})

    def publish(self, url, text):
        vid = video_id(url)
        if not isinstance(text, str) or not text.strip() or len(text) > 180 or any(c in text for c in '@＠'):
            raise DouyinAPIError('DOUYIN_INVALID_COMMENT', before_write=True)
        return self.call('publish', {'video_id': vid, 'text': text})

    def mention_target(self, uid):
        return self.call('mention_target', dict(uid=uid))

    def publish_mention(self, url, text, uid):
        vid = video_id(url)
        if not isinstance(text, str) or not text.strip() or len(text) > 180:
            raise DouyinAPIError('DOUYIN_INVALID_COMMENT', before_write=True)
        return self.call('publish_mention', dict(video_id=vid, text=text, mention_uid=uid))


class APIVideoBrowser:
    """VideoCurator interface with no Page, navigation or claimed playback."""
    platform = 'douyin'
    transport = 'api'
    page = None

    def __init__(self, api, *, max_videos=5, max_source_seconds=300, filter_long=True, snapshot_path=None):
        self.api, self.max_videos = api, max_videos
        # 0 or None disables the duration filter entirely; sampling stays bounded by analysis_seconds.
        self.max_source_seconds = max_source_seconds or 0
        self.unlimited = not self.max_source_seconds
        self.candidates, self.visited, self.played = {}, [], set()
        self.current, self.current_detail = None, None
        self.index = 1
        self.filter_long, self.snapshot_path = filter_long, Path(snapshot_path) if snapshot_path else None
        self.batch_rows = []
        self.live_skipped = 0

    @staticmethod
    def _not_a_video(row):
        """Live rooms and non-video cards must never be treated as a video to open."""
        if not isinstance(row, dict):
            return True
        if row.get('live') or row.get('cell_room') or row.get('room_id'):
            return True
        return False

    def _batch(self, rows):
        if not isinstance(rows, list):
            raise DouyinAPIError('DOUYIN_BAD_FEED')
        self.candidates = {}
        self.batch_rows = []
        for row in rows[:50]:
            if not isinstance(row, dict) or not positive(row.get('aweme_id')):
                continue
            if self._not_a_video(row):
                self.live_skipped += 1
                continue
            duration = (row.get('video') or {}).get('duration') or row.get('duration')
            if self.filter_long and not self.unlimited and isinstance(duration, (int, float)) and duration > self.max_source_seconds*1000:
                continue
            self.candidates['https://www.douyin.com/video/'+str(row['aweme_id'])] = str(row.get('desc', ''))[:300]
            self.batch_rows.append(dict(aweme_id=str(row['aweme_id']), desc=str(row.get('desc', ''))[:300], duration=duration))
        return self.observe()

    def recommend(self):
        self.current = self.current_detail = None
        self.index += 1
        observation = self._batch(self.api.feed(self.index))
        if self.snapshot_path:
            data = dict(account=self.api.uid, index=self.index, rows=self.batch_rows, saved_at=time.time())
            temporary = self.snapshot_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.snapshot_path)
        return observation

    def initial_recommendations(self):
        if self.snapshot_path and self.snapshot_path.is_file() and self.snapshot_path.stat().st_size < 100000:
            try:
                data = json.loads(self.snapshot_path.read_text(encoding='utf-8'))
                if (data.get('account') == self.api.uid and isinstance(data.get('rows'), list)
                        and type(data.get('index')) is int and 0 <= time.time()-data.get('saved_at', 0) < 3600):
                    self.index = data['index']
                    observation = self._batch(data['rows'])
                    self.snapshot_path.unlink()
                    return observation
            except (OSError, ValueError, TypeError):
                pass
        observation = self.recommend()
        if self.snapshot_path:
            self.snapshot_path.unlink(missing_ok=True)
        return observation

    def open(self, topic=''):
        self.current = self.current_detail = None
        return self._batch(self.api.search(topic)) if topic else self.recommend()

    def next(self, url=''):
        from ...modules.video import VideoError
        url = url or next((u for u in self.candidates if u not in self.visited), '')
        if len(self.visited) >= self.max_videos or url not in self.candidates or url in self.visited:
            raise VideoError('video_browser_no_new_api_video')
        detail = self.api.detail(url)
        if self._not_a_video(detail) or not isinstance(detail.get('video'), dict):
            raise DouyinAPIError('DOUYIN_VIDEO_UNAVAILABLE', before_write=True)
        duration = (detail.get('video') or {}).get('duration') or detail.get('duration')
        if not self.filter_long:
            if type(duration) not in (int,float) or not math.isfinite(duration) or duration <= 0:
                raise DouyinAPIError('DOUYIN_VIDEO_DURATION_UNKNOWN', before_write=True)
            if not self.unlimited and duration > self.max_source_seconds*1000:
                raise DouyinAPIError('DOUYIN_VIDEO_TOO_LONG', before_write=True)
        self.current, self.current_detail = url, detail
        self.visited.append(url)
        return self.observe()

    def observe(self):
        detail = self.current_detail or {}
        duration = (detail.get('video') or {}).get('duration') or detail.get('duration')
        return dict(platform='douyin', transport='api', page_title=str(detail.get('desc', ''))[:500],
            video=dict(duration=float(duration)/1000, paused=True, current_time=0) if duration else None,
            candidates=[dict(url=u, title=t, visited=u in self.visited) for u, t in self.candidates.items()],
            current=self.current, remaining=self.max_videos-len(self.visited), blocked=False,
            playback_confirmed=False, live_skipped=self.live_skipped)

    def media_address(self):
        from ...video_evidence import media_host
        if not self.current_detail or str(self.current_detail.get('aweme_id')) != video_id(self.current):
            raise DouyinAPIError('DOUYIN_VIDEO_MISMATCH')
        video = self.current_detail.get('video') or {}
        variants = [video.get(key) for key in ('play_addr', 'play_addr_h264', 'play_addr_265')]
        variants += [r.get('play_addr') for r in video.get('bit_rate', []) if isinstance(r, dict)]
        choices = []
        from urllib.parse import urlsplit
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            # Use a smaller actual rendition rather than downloading the default 1080p source.
            size = variant.get('data_size')
            size = size if type(size) is int and size > 0 else float('inf')
            for address in variant.get('url_list') or []:
                if isinstance(address, str) and media_host(address, 'douyin'):
                    # Prefer CDN files. The www play endpoint redirects and the downloader refuses redirects.
                    redirect = urlsplit(address).hostname in ('www.douyin.com', 'douyin.com')
                    choices.append((redirect, size, address))
        return min(choices, key=lambda c: (c[0], c[1]))[2] if choices else ''

    def audio_address(self):
        from ...video_evidence import media_host
        if not self.current_detail or str(self.current_detail.get('aweme_id')) != video_id(self.current):
            raise DouyinAPIError('DOUYIN_VIDEO_MISMATCH')
        choices = []
        for variant in (self.current_detail.get('video') or {}).get('bit_rate_audio') or []:
            if not isinstance(variant, dict):
                continue
            meta = variant.get('audio_meta') or {}
            urls = meta.get('url_list') or {}
            urls = list(urls.values()) if isinstance(urls, dict) else urls
            size = meta.get('size')
            size = size if type(size) is int and size > 0 else float('inf')
            for url in urls:
                if isinstance(url, str) and media_host(url, 'douyin'):
                    choices.append((size, url))
        return min(choices, key=lambda c: c[0])[1] if choices else ''
