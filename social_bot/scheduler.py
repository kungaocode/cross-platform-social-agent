"""跨进程共享的模型请求并发池。SQLite 仅保存调度令牌，不保存请求正文或密钥。"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager


class SchedulerBusy(RuntimeError):
    pass


class Lease:
    def __init__(self, pool, token, admitted_at=None):
        self.pool, self.token = pool, token
        self.admitted_at=time.time() if admitted_at is None else admitted_at
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._renew, daemon=True, name='model-lease-heartbeat')
        self.thread.start()

    def _renew(self):
        while not self.stop.wait(10):
            try:
                with self.pool.connect() as db:
                    db.execute('UPDATE leases SET expires=? WHERE token=?', (time.time()+self.pool.ttl,self.token))
            except sqlite3.Error:
                pass

    def close(self):
        if self.stop.is_set(): return
        self.stop.set()
        self.thread.join(timeout=1)
        with self.pool.connect() as db: db.execute('DELETE FROM leases WHERE token=?',(self.token,))


class RequestPool:
    def __init__(self, path, *, concurrency=2, interval=1.0, max_queue=32, wait_seconds=90, ttl=60):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.concurrency=max(1,min(16,int(concurrency)))
        self.interval=max(0,min(60,float(interval)))
        self.max_queue=max(1,min(512,int(max_queue)))
        self.wait_seconds=max(1,min(300,float(wait_seconds)))
        self.ttl=max(30,float(ttl))
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS leases(token TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS waiting(id INTEGER PRIMARY KEY AUTOINCREMENT,token TEXT UNIQUE,expires REAL);
                CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY, next_start REAL);
                INSERT OR IGNORE INTO schedule VALUES(1,0);''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5)
        try:
            with db:yield db
        finally:db.close()

    def acquire(self):
        token=uuid.uuid4().hex; deadline=time.monotonic()+self.wait_seconds
        granted=False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM waiting WHERE expires<?',(time.time(),))
            if db.execute('SELECT COUNT(*) FROM waiting').fetchone()[0]>=self.max_queue:
                raise SchedulerBusy('模型请求等待队列已满，请稍后重试。')
            db.execute('INSERT INTO waiting(token,expires) VALUES(?,?)',(token,time.time()+self.wait_seconds+2))
        try:
            while time.monotonic()<deadline:
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    now=time.time()  # Lock contention must not use a stale admission timestamp.
                    db.execute('DELETE FROM leases WHERE expires<?',(now,))
                    db.execute('DELETE FROM waiting WHERE expires<?',(now,))
                    head=db.execute('SELECT token FROM waiting ORDER BY id LIMIT 1').fetchone()
                    active=db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]
                    next_start=db.execute('SELECT next_start FROM schedule WHERE id=1').fetchone()[0]
                    if head and head[0]==token and active<self.concurrency and now>=next_start:
                        db.execute('DELETE FROM waiting WHERE token=?',(token,))
                        db.execute('INSERT INTO leases VALUES(?,?)',(token,now+self.ttl))
                        db.execute('UPDATE schedule SET next_start=? WHERE id=1',(now+self.interval,))
                        db.commit()
                        granted=True
                        return Lease(self,token,now)
                time.sleep(0.1)
            raise SchedulerBusy('模型请求排队超时，请稍后重试。')
        finally:
            if not granted:
                with self.connect() as db: db.execute('DELETE FROM waiting WHERE token=?',(token,))


class ScheduledResponse:
    def __init__(self,response,lease): self.response,self.lease=response,lease
    def __getattr__(self,name): return getattr(self.response,name)
    def __iter__(self): return iter(self.response)
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self):
        try:self.response.close()
        finally:self.lease.close()


def configured_pool():
    root=Path(__file__).resolve().parent.parent
    path=root/'scheduler.json'
    config=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if config.get('enabled',True) is False:return None
    database=Path(config.get('database_path',str(root/'data'/'scheduler.sqlite3')))
    if not database.is_absolute():database=root/database
    return RequestPool(database,concurrency=config.get('max_concurrent_requests',2),
                       interval=config.get('min_start_interval_seconds',1),max_queue=config.get('max_queue',32),
                       wait_seconds=config.get('queue_timeout_seconds',90))
