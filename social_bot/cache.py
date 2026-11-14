"""Managed media cache, retained until the operator runs clean_cache.py.

Only marked cache roots can be cleared. No symlink/junction is traversed and
live task directories are protected by OS file locks, including after crashes.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextvars import ContextVar
from functools import wraps
from contextlib import contextmanager
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MARKER = '.social-bot-cache.json'


def configured_cache_root():
    if os.environ.get('SOCIAL_BOT_CACHE_DIR'):
        return Path(os.environ['SOCIAL_BOT_CACHE_DIR']).resolve()
    config = PROJECT/'config.json'
    raw = json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    runtime=raw.get('runtime',{})
    root = Path(runtime.get('cache_directory',PROJECT/'data/cache'))
    root=(PROJECT/root).resolve() if not root.is_absolute() else root.resolve()
    database=Path(runtime.get('database_path',PROJECT/'data/bot.sqlite3'))
    database=(PROJECT/database).resolve() if not database.is_absolute() else database.resolve()
    if database.is_relative_to(root):raise ValueError('cache directory cannot contain the memory database')
    return root


def _reparse(path):
    return path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())


@contextmanager
def _lock(path, *, blocking=True):
    import errno
    if _reparse(path) or _reparse(path.parent):
        raise ValueError('cache lock cannot follow a reparse point')
    path.parent.mkdir(parents=True,exist_ok=True)
    file = path.open('a+b')
    try:
        file.seek(0)
        if os.fstat(file.fileno()).st_size == 0:
            file.write(b'0');file.flush()
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(file.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise BlockingIOError(errno.EAGAIN,'cache is in use') from exc
        else:
            import fcntl
            fcntl.flock(file,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(file,fcntl.LOCK_UN)
    finally:
        file.close()


class MediaCache:
    def __init__(self, root=None):
        given = Path(root) if root is not None else configured_cache_root()
        if _reparse(given):
            raise ValueError('cache root cannot be a link or junction')
        self.root = given.resolve()
        if self.root in (Path(self.root.anchor),PROJECT,Path.home().resolve()):
            raise ValueError('cache root must be a dedicated directory')
        self.root.mkdir(parents=True,exist_ok=True)
        marker = self.root/MARKER
        with _lock(self.root/'.cache.lock'):
            if _reparse(marker):raise ValueError('cache marker cannot be a link')
            if not marker.exists():
                if any(p.name!='.cache.lock' for p in self.root.iterdir()):
                    raise ValueError('unmarked nonempty directory is not a managed cache')
                marker.write_text(json.dumps(dict(kind='social-bot-cache-v1',root=str(self.root))),encoding='utf-8')
            data = json.loads(marker.read_text(encoding='utf-8'))
        if data.get('kind') != 'social-bot-cache-v1' or data.get('root') != str(self.root):
            raise ValueError('cache root marker does not match')

    @contextmanager
    def task(self, category='media'):
        if not category.replace('-','').isalnum():
            raise ValueError('invalid cache category')
        with _lock(self.root/'.cache.lock'):
            folder = Path(tempfile.mkdtemp(prefix=category+'-',dir=self.root))
            active = _lock(folder/'.active')
            active.__enter__()
        try:
            yield str(folder)
        finally:
            active.__exit__(None,None,None)

    def clear(self, *, dry_run=False):
        report = dict(root=str(self.root),dry_run=dry_run,files=0,bytes=0,
                      skipped_active=[],errors=[])
        def remove(path):
            try:
                # Check reparse points before resolving, and remove the link only.
                if _reparse(path):
                    if not dry_run:
                        if path.is_symlink():path.unlink()
                        else:path.rmdir()
                    report['files']+=1
                    return
                resolved=path.resolve(strict=True)
                if resolved == self.root or not resolved.is_relative_to(self.root):
                    raise ValueError('cache cleanup target escaped root')
                if path.is_dir():
                    for child in path.iterdir():remove(child)
                    if not dry_run:path.rmdir()
                else:
                    report['bytes']+=path.stat().st_size
                    report['files']+=1
                    if not dry_run:path.unlink()
            except OSError as exc:
                report['errors'].append(dict(path=str(path),error=type(exc).__name__))
        with _lock(self.root/'.cache.lock'):
            if _reparse(self.root/MARKER):raise ValueError('cache marker cannot be a link')
            data=json.loads((self.root/MARKER).read_text(encoding='utf-8'))
            if data.get('kind')!='social-bot-cache-v1' or data.get('root')!=str(self.root):
                raise ValueError('cache marker changed; no files removed')
            for path in self.root.iterdir():
                if path.name in (MARKER,'.cache.lock'):continue
                if path.is_dir() and not _reparse(path) and (path/'.active').exists():
                    try:
                        with _lock(path/'.active',blocking=False):pass
                    except BlockingIOError:
                        report['skipped_active'].append(path.name)
                        continue
                remove(path)
        return report


@contextmanager
def cache_task(category='media', *, root=None):
    with MediaCache(root).task(category) as path:
        yield path


class CacheOwner:
    """Per-request cache location without mutating a shared module across threads."""
    @property
    def cache_dir(self):
        return self._cache_context.get() or self._cache_base

    @cache_dir.setter
    def cache_dir(self,value):
        self._cache_base=Path(value)
        self._cache_context=ContextVar('media-cache-folder',default=None)

    @contextmanager
    def media_task(self,category='media'):
        if self._cache_context.get() is not None:
            yield self.cache_dir
            return
        root=self._cache_base
        # Legacy cache contents are retained, never silently registered for deletion.
        if root.exists() and not (root/MARKER).exists() and any(root.iterdir()):
            root=root/'managed'
        with cache_task(category,root=root) as folder:
            token=self._cache_context.set(Path(folder))
            try:yield Path(folder)
            finally:self._cache_context.reset(token)


def retained_media(category):
    def decorate(method):
        @wraps(method)
        def retained(self,*args,**kwargs):
            with self.media_task(category):return method(self,*args,**kwargs)
        return retained
    return decorate
