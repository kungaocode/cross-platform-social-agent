"""Bounded online video -> temporary MP4 -> shared audio/visual analyzer.

Downloader runs in a killable subprocess. Reports never contain cookies or signed media URLs.
"""
from __future__ import annotations

from social_bot.cache import cache_task

import argparse
import http.cookiejar
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler

from .modules.video import OmniVideoAnalyzer, VideoError


def platform_url(value):
    value = value.strip()
    if re.fullmatch(r'BV[A-Za-z0-9]{10}', value):
        value = 'https://www.bilibili.com/video/' + value
    url = urlsplit(value)
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise VideoError('online_video_requires_platform_https_url')
    host = (url.hostname or '').lower()
    if host in ('www.bilibili.com', 'bilibili.com') and re.fullmatch(r'/video/BV[A-Za-z0-9]{10}/?', url.path):
        return 'bilibili', value
    if host in ('www.douyin.com', 'douyin.com', 'www.iesdouyin.com') and re.fullmatch(r'/(?:video|share/video)/\d+/?', url.path):
        return 'douyin', value
    if host in ('b23.tv', 'v.douyin.com') and re.fullmatch(r'/[A-Za-z0-9_-]+/?', url.path):
        return ('bilibili' if host == 'b23.tv' else 'douyin'), value
    raise VideoError('online_video_unsupported_url')


def write_cookies(path, platform, cookies):
    jar = http.cookiejar.MozillaCookieJar(str(path))
    suffix = '.bilibili.com' if platform == 'bilibili' else '.douyin.com'
    for entry in cookies:
        domain = entry.get('domain', '')
        if domain.lstrip('.') != suffix[1:] and not domain.endswith(suffix):
            continue
        jar.set_cookie(http.cookiejar.Cookie(0, entry['name'], entry['value'], None, False,
            domain, domain.startswith('.'), domain.startswith('.'), entry.get('path', '/'), True,
            bool(entry.get('secure', True)), None, True, None, None, {}, False))
    jar.save(ignore_discard=True, ignore_expires=True)


def local_cookies(platform):
    if platform != 'bilibili':
        return []
    path = Path(__file__).resolve().parent.parent/'bilibili/credentials.json'
    private_path = path.parent.parent/'private/accounts/bilibili/credentials.json'
    if private_path.is_file():
        path = private_path
    saved = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    token = os.environ.get('BILIBILI_SESSDATA', saved.get('SESSDATA', ''))
    return [dict(name='SESSDATA', value=token, domain='.bilibili.com', path='/', secure=True)] if token else []


def resolve_video_url(value):
    platform, value = platform_url(value)
    if urlsplit(value).hostname not in ('b23.tv', 'v.douyin.com'):
        return value
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if platform_url(newurl)[0] != platform:
                raise VideoError('online_video_cross_platform_redirect')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    with build_opener(ProxyHandler({}), Redirect()).open(Request(value, headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as response:
        resolved=response.geturl()
    if urlsplit(resolved).hostname in ('b23.tv', 'v.douyin.com'):
        raise VideoError('online_video_short_url_not_resolved')
    platform_url(resolved)
    return resolved


def _kill(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


class OnlineVideoAnalyzer:
    def __init__(self, analyzer, *, timeout=240, max_download_bytes=400*1024*1024):
        self.analyzer, self.timeout, self.max_download_bytes = analyzer, timeout, max_download_bytes

    def analyze(self, url, question='', *, start=0, seconds=180, cookies=()):
        platform, url = platform_url(url)
        if self.analyzer is None:
            raise VideoError('video_model_not_configured')
        if not isinstance(start, int) or start < 0 or not isinstance(seconds, int) or not 2 <= seconds <= 290:
            raise VideoError('online_video_invalid_time_range')
        with cache_task('online-video') as folder, tempfile.TemporaryDirectory(prefix='deepseek-credentials-') as secrets:
            root = Path(folder)
            cookie_file = Path(secrets)/'cookies.txt'
            write_cookies(cookie_file, platform, cookies)
            command = [sys.executable, '-m', 'social_bot.online_video', url, str(root),
                       '--start', str(start), '--seconds', str(seconds), '--cookies', str(cookie_file)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt',
                cwd=Path(__file__).resolve().parent.parent)
            deadline = time.monotonic()+self.timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise VideoError('online_video_download_timeout')
                    if sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) > self.max_download_bytes:
                        raise VideoError('online_video_download_size_limit')
                    time.sleep(.2)
            finally:
                if process.poll() is None:
                    _kill(process)
            if process.returncode != 0 or not (root/'result.json').is_file():
                if (root/'error.json').is_file():
                    raise VideoError(json.loads((root/'error.json').read_text())['reason'])
                raise VideoError('online_video_unavailable_login_or_platform_restriction')
            metadata = json.loads((root/'result.json').read_text(encoding='utf-8'))
            media = root/'media.mp4'
            if not media.is_file() or media.stat().st_size > OmniVideoAnalyzer.MAX_SOURCE_BYTES:
                raise VideoError('online_video_mp4_not_available')
            result = self.analyzer.analyze_path(media, question)
            actual = result['coverage_seconds']
            duration = metadata.get('duration_seconds')
            partial = bool(start or duration is None or duration > actual+1)
            return dict(success=True, platform=platform, url=url, title=metadata['title'],
                author=metadata['author'], source_duration_seconds=duration,
                coverage_start_seconds=start, coverage_end_seconds=round(start+actual, 2),
                partial=partial, evidence='audio_visual', text=result['text'],
                note=('仅解析所标时间范围，不能当作全片结论。' if partial else '已解析此视频完整可用片段。') + result['note'],
                has_audio=result.get('has_audio'), model=result['model'], summary_model=result.get('summary_model',result['model']))


def worker(url, folder, start, seconds, cookies=None):
    import yt_dlp
    from yt_dlp.utils import download_range_func
    url = resolve_video_url(url)
    root = Path(folder)
    options = dict(quiet=True, no_warnings=True, noplaylist=True, retries=0, fragment_retries=0, proxy='',
        allowed_extractors=['bilibili', 'douyin'],
        socket_timeout=20, cookiefile=str(cookies or root/'cookies.txt'),
        ffmpeg_location=OmniVideoAnalyzer._ffmpeg(), outtmpl=str(root/'media.%(ext)s'),
        format='bv[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]',
        merge_output_format='mp4', max_filesize=OmniVideoAnalyzer.MAX_SOURCE_BYTES,
        playlist_items='1', cachedir=False)
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        if not info or info.get('_type') in ('playlist', 'multi_video') or info.get('is_live'):
            raise VideoError('online_video_requires_single_nonlive_video')
        duration = info.get('duration')
        if duration is not None and start >= duration:
            raise VideoError('online_video_start_after_end')
        if start or duration is None or duration > seconds:
            downloader.params['download_ranges'] = download_range_func(None, [(start, start+seconds)])
            downloader.params['force_keyframes_at_cuts'] = True
        downloader.process_ie_result(info, download=True)
    if not (root/'media.mp4').is_file():
        matches = list(root.glob('media*.mp4'))
        if len(matches) == 1:
            matches[0].replace(root/'media.mp4')
    result = dict(title=str(info.get('title', ''))[:500], author=str(info.get('uploader', ''))[:200], duration_seconds=duration)
    (root/'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('folder', type=Path)
    parser.add_argument('--cookies', type=Path)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--seconds', type=int, default=180)
    args = parser.parse_args()
    try:
        worker(args.url, args.folder, args.start, args.seconds, args.cookies)
    except Exception as exc:
        text = str(exc)
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        http = re.search(r'HTTP(?: Error| error)?[^0-9]*(\d{3})', text)
        code = ('online_video_http_'+http[1]) if http else (
            str(exc) if isinstance(exc, VideoError) else 'online_video_'+type(exc).__name__)
        (args.folder/'error.json').write_text(json.dumps({'reason':code}), encoding='utf-8')
        raise SystemExit(1)
