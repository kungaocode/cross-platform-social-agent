"""A dedicated persistent browser; each search runs in a deadline-bounded child."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid

from .duckduckgo import DuckDuckGoError, DuckDuckGoSearch

ROOT = Path(__file__).resolve().parents[3]
TAB_NAME = 'social-bot-duckduckgo-search'


@contextmanager
def profile_lock(path, deadline):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        locked = False
        try:
            while time.monotonic() < deadline:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    time.sleep(min(.03, max(0, deadline-time.monotonic())))
            if not locked:
                raise DuckDuckGoError('duckduckgo_timeout')
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def browser_executable(channel):
    candidates = []
    if os.name == 'nt':
        suffix = 'Microsoft/Edge/Application/msedge.exe' if channel == 'msedge' else 'Google/Chrome/Application/chrome.exe'
        for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name])/suffix)
    else:
        for name in (('microsoft-edge', 'microsoft-edge-stable') if channel == 'msedge' else ('google-chrome', 'chromium', 'chromium-browser')):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise DuckDuckGoError('duckduckgo_browser_not_installed')


class DuckDuckGoBrowserSearch:
    def __init__(self, profile, *, channel='msedge', timeout_seconds=15):
        self.profile = Path(profile).expanduser().resolve()
        self.channel = channel
        self.timeout_seconds = max(.05, min(15., float(timeout_seconds)))
        if channel not in {'msedge', 'chrome'}:
            raise ValueError('unsupported_duckduckgo_browser_channel')

    def _endpoint(self, deadline):
        """Only accept the loopback debugger matching this profile's browser UUID."""
        try:
            lines = (self.profile/'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
            port, browser_path = int(lines[0]), lines[1]
            if not 1 <= port <= 65535 or not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', browser_path):
                return None
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return None
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=min(.2, remaining))
            try:
                connection.request('GET', '/json/version')
                response = connection.getresponse()
                info = json.loads(response.read(16384))
            finally:
                connection.close()
            actual = urlsplit(info.get('webSocketDebuggerUrl', ''))
            if actual.hostname != '127.0.0.1' or actual.port != port or actual.path != browser_path:
                return None
            return f'http://127.0.0.1:{port}', browser_path
        except (OSError, ValueError, IndexError, http.client.HTTPException):
            return None

    def _ensure_browser(self, deadline, *, start=True):
        self.profile.mkdir(parents=True, exist_ok=True)
        with profile_lock(self.profile/'.launch.lock', deadline):
            endpoint = self._endpoint(deadline)
            if endpoint:
                return endpoint
            if not start:
                raise DuckDuckGoError('duckduckgo_browser_not_running')
            if time.monotonic() >= deadline:
                raise DuckDuckGoError('duckduckgo_timeout')
            executable = browser_executable(self.channel)
            process = subprocess.Popen([executable, '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                '--user-data-dir='+str(self.profile), '--no-first-run', '--no-default-browser-check', '--disable-sync',
                '--no-proxy-server', '--new-window', 'https://duckduckgo.com/'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            # Retain a handle without waiting for browser shutdown; its lifetime is the visible session.
            self._launched_browser = process
            while time.monotonic() < deadline:
                endpoint = self._endpoint(deadline)
                if endpoint:
                    return endpoint
                time.sleep(min(.04, max(0, deadline-time.monotonic())))
        raise DuckDuckGoError('duckduckgo_timeout')

    @staticmethod
    def _stop_worker(process, deadline):
        # Edge/Chrome is launched by the caller, outside this disposable Python/Playwright tree.
        if process.poll() is None:
            if os.name == 'nt':
                killer = subprocess.Popen(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    killer.wait(timeout=max(.001, min(.1, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def _exchange(self, command, query='', limit=3):
        deadline = time.monotonic()+self.timeout_seconds
        endpoint, session = self._ensure_browser(deadline, start=command not in {'status', 'stop'})
        remaining = deadline-time.monotonic()
        if remaining <= .2:
            raise DuckDuckGoError('duckduckgo_timeout')
        payload = dict(command=command, query=query, limit=max(1, min(8, int(limit))), profile=str(self.profile),
                       endpoint=endpoint, session=session, expires_at=time.time()+remaining-.2)
        process = subprocess.Popen([sys.executable, '-m', 'social_bot.modules.research.duckduckgo_browser', '--worker'],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
            start_new_session=os.name != 'nt')
        try:
            output, _ = process.communicate(json.dumps(payload).encode('utf-8'), timeout=max(.001, deadline-time.monotonic()-.2))
        except subprocess.TimeoutExpired:
            self._stop_worker(process, deadline)
            raise DuckDuckGoError('duckduckgo_timeout') from None
        if time.monotonic() > deadline:
            raise DuckDuckGoError('duckduckgo_timeout')
        try:
            if len(output) > 1024*1024:
                raise ValueError()
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise DuckDuckGoError('duckduckgo_browser_worker_error') from None
        if result.get('ok') is not True:
            raise DuckDuckGoError(result.get('reason', 'duckduckgo_browser_error'),
                http_status=result.get('http_status'), cooldown_seconds=result.get('cooldown_seconds', 60))
        return result

    def search(self, query, limit):
        return self._exchange('search', query, limit)['rows']

    def prepare(self, query='Python documentation'):
        return self._exchange('prepare', query)

    def status(self):
        return self._exchange('status')

    def stop(self):
        return self._exchange('stop')

    def recovery_token(self):
        try:
            data = json.loads((self.profile/'.verified.json').read_text(encoding='utf-8'))
            token = data.get('token', '') if isinstance(data, dict) else ''
            return token if isinstance(token, str) and len(token) <= 128 else ''
        except (OSError, ValueError):
            return ''


def duck_origin(url):
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    return parts.scheme == 'https' and (host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'))


async def page_state(page):
    if not duck_origin(page.url):
        return 'pending'
    text = await page.content()
    if any(marker in text.lower() for marker in ('anomaly.js', 'challenge-form', 'bots use duckduckgo')):
        return 'verification_required'
    try:
        rows = DuckDuckGoSearch.parse(text, page.url, 3)
        return 'ready' if rows else 'empty'
    except DuckDuckGoError:
        return 'pending'


async def search_page(page, query, limit, expires_at):
    if await page_state(page) == 'verification_required':
        raise DuckDuckGoError('duckduckgo_captcha_required', cooldown_seconds=600)
    remaining = expires_at-time.time()
    if remaining <= 0:
        raise DuckDuckGoError('duckduckgo_timeout')
    url = DuckDuckGoSearch.ENDPOINTS[0]+'?'+urlencode({'q': query, 'kl': 'us-en'})
    response = await page.goto(url, wait_until='domcontentloaded', timeout=max(1, remaining*1000))
    status = response.status if response else None
    while time.time() < expires_at:
        if not duck_origin(page.url):
            raise DuckDuckGoError('duckduckgo_browser_left_search')
        html = await page.content()
        if len(html) > 2*1024*1024:
            raise DuckDuckGoError('duckduckgo_response_invalid')
        if any(marker in html.lower() for marker in ('anomaly.js', 'challenge-form', 'bots use duckduckgo')):
            raise DuckDuckGoError('duckduckgo_captcha_required', http_status=status, cooldown_seconds=600)
        if status in {403, 429}:
            raise DuckDuckGoError('duckduckgo_rate_limited' if status == 429 else 'duckduckgo_access_denied',
                                  http_status=status, cooldown_seconds=300)
        if status is not None and status >= 400:
            raise DuckDuckGoError('duckduckgo_http_error', http_status=status)
        try:
            return DuckDuckGoSearch.parse(html, page.url, limit)
        except DuckDuckGoError:
            await asyncio.sleep(min(.1, max(0, expires_at-time.time())))
    raise DuckDuckGoError('duckduckgo_timeout')


async def worker_async(payload):
    from playwright.async_api import async_playwright
    profile = Path(payload['profile'])
    remaining = payload['expires_at']-time.time()
    if remaining <= 0:
        raise DuckDuckGoError('duckduckgo_timeout')
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(payload['endpoint'], timeout=max(1, remaining*1000))
        context = browser.contexts[0]
        if payload['command'] == 'stop':
            await browser.close()
            return dict(ok=True, state='stopped')
        pages = [p for p in context.pages if duck_origin(p.url)]
        page = None
        for candidate in pages:
            if await candidate.evaluate('window.name') == TAB_NAME:
                page = candidate
                break
        if page is None:
            page = await context.new_page()
        # Restore session cookies only once per browser, never overwrite a newly completed challenge.
        session_path = profile/'.session.json'
        try:
            saved = json.loads(session_path.read_text(encoding='utf-8'))
            if saved.get('restored_for') != payload['session']:
                cookies = [c for c in saved.get('cookies', []) if str(c.get('domain', '')).lstrip('.') == 'duckduckgo.com'
                           or str(c.get('domain', '')).endswith('.duckduckgo.com')]
                if cookies:
                    await context.add_cookies(cookies)
                saved['restored_for'] = payload['session']
                atomic_json(session_path, saved)
        except (OSError, ValueError, TypeError):
            pass
        await page.evaluate('(name) => { window.name = name; }', TAB_NAME)
        if payload['command'] == 'prepare':
            await page.bring_to_front()
        if payload['command'] == 'status':
            result = dict(ok=True, state=await page_state(page))
        else:
            try:
                rows = await search_page(page, payload['query'], payload['limit'], payload['expires_at'])
                result = dict(ok=True, rows=rows, state='ready' if rows else 'empty')
            except DuckDuckGoError as exc:
                if payload['command'] != 'prepare' or exc.reason != 'duckduckgo_captcha_required':
                    raise
                result = dict(ok=True, state='verification_required')
            finally:
                if duck_origin(page.url):
                    await page.evaluate('(name) => { window.name = name; }', TAB_NAME)
        if payload['expires_at'] > time.time():
            try:
                cookies = await context.cookies(['https://duckduckgo.com/', 'https://html.duckduckgo.com/'])
                atomic_json(session_path, dict(cookies=cookies, restored_for=payload['session']))
            except Exception:
                pass
        if result.get('state') == 'ready':
            atomic_json(profile/'.verified.json', dict(token=uuid.uuid4().hex, time=time.time()))
        result['session_id'] = payload['session'].rsplit('/', 1)[-1]
        # Stopping Playwright disconnects the worker; the separately launched browser stays open.
        return result


def worker_main():
    try:
        payload = json.loads(sys.stdin.buffer.read(16384))
        endpoint = urlsplit(payload['endpoint'])
        if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or not endpoint.port
                or payload['command'] not in {'search', 'prepare', 'status', 'stop'}
                or not isinstance(payload.get('query'), str) or len(payload['query']) > 500):
            raise ValueError()
        remaining = min(15, payload['expires_at']-time.time())
        with profile_lock(Path(payload['profile'])/'.search.lock', time.monotonic()+max(0, remaining)):
            result = asyncio.run(asyncio.wait_for(worker_async(payload), timeout=max(.001, payload['expires_at']-time.time())))
    except DuckDuckGoError as exc:
        result = dict(ok=False, reason=exc.reason, http_status=exc.http_status, cooldown_seconds=exc.cooldown_seconds)
    except (asyncio.TimeoutError, TimeoutError):
        result = dict(ok=False, reason='duckduckgo_timeout')
    except Exception as exc:
        result = dict(ok=False, reason='duckduckgo_timeout' if type(exc).__name__ == 'TimeoutError' else 'duckduckgo_browser_error')
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__' and '--worker' in sys.argv:
    worker_main()
