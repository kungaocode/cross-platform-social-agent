"""Transactional local outbox -> one shared agent memory. Transport state stays local."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .agent_memory import AgentMemory

LOGGER = logging.getLogger(__name__)


class AgentReplicaMixin:
    def _init_agent_replica(self):
        self.agent_memory = None
        self.agent_account = 'default'
        self._agent_source = ''
        self._agent_flush_lock = threading.RLock()
        self._agent_last_error = ''
        self._agent_retry_stop = threading.Event()
        self._agent_retry_thread = None

    def start_agent_retry(self, seconds):
        if self.agent_memory is None or seconds <= 0 or self._agent_retry_thread is not None:
            return
        self._agent_retry_stop.clear()

        def retry():
            while not self._agent_retry_stop.wait(seconds):
                try:
                    # Bound each pass so closing a busy store can stop between batches.
                    self.flush_agent_memory(max_batches=1)
                except Exception as exc:
                    self._agent_last_error = type(exc).__name__
                    LOGGER.warning('共享记忆定时补同步失败，待办仍保留，错误类型=%s',self._agent_last_error)

        self._agent_retry_thread = threading.Thread(target=retry, name='agent-memory-retry', daemon=True)
        self._agent_retry_thread.start()

    def stop_agent_retry(self):
        self._agent_retry_stop.set()
        worker = self._agent_retry_thread
        if worker is not None and worker is not threading.current_thread():
            # No store lock is held here; let an in-flight SQLite transaction finish.
            worker.join()
        self._agent_retry_thread = None

    def configure_agent_memory(self, path, *, account='default', backfill=True):
        if not path:
            return
        if Path(path).expanduser().resolve() == self.database_path:
            raise ValueError('共享记忆库不能与平台传输数据库使用同一路径')
        self.agent_memory = AgentMemory(path)
        with self._lock, self.connection:
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS agent_replica_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_outbox(id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS idx_agent_outbox ON agent_outbox(state,id);
            ''')
            for key, value in [('source_id',uuid.uuid4().hex),('account_id',str(account))]:
                self.connection.execute('INSERT OR IGNORE INTO agent_replica_meta VALUES (?,?)',(key,value))
            meta = dict(self.connection.execute('SELECT key,value FROM agent_replica_meta'))
            self._agent_source, self.agent_account = meta['source_id'], meta['account_id']
        if backfill:
            self.backfill_agent_memory()
        self.flush_agent_memory()

    def _agent_meta(self, session, sender_id=''):
        parts = session.split(':')
        platform, account, channel = parts[0], self.agent_account, 'private'
        if len(parts) >= 3 and parts[1] in {'private','group'}:
            channel = 'comment' if parts[1] == 'group' and ':comment:' in session else parts[1]
            if not sender_id and channel == 'private':
                sender_id = ':'.join(parts[2:])
        elif platform == 'bilibili' and len(parts) >= 3:
            account, channel = parts[1], 'activity'
        elif platform == 'video-agent' and len(parts) >= 3:
            platform, account, channel = parts[1], parts[2], 'activity'
        elif platform == 'video' and len(parts) >= 4:
            # Existing video sessions used video:<platform>:<platform>:<account>:<url>.
            account = parts[3] if parts[2] == parts[1] else parts[2]
            if account == 'direct':
                account = 'default'
            platform, channel = parts[1], 'video'
        return dict(source_id=self._agent_source, source_session=session, platform=platform, account=account,
                    channel=channel, sender_id=sender_id or 'unknown:' + session)

    def _queue_agent_event(self, row_id, session, event, now):
        if self.agent_memory is None:
            return
        content = event.get('memory_content') if event['role']=='user' else None
        content = event['content'] if content is None else content
        meta = self._agent_meta(session,event.get('sender_id',''))
        if meta['channel'] == 'video':
            try:
                evidence = json.loads(content)
                is_video = event['role'] == 'assistant' and isinstance(evidence, dict) and evidence.get('success') is True
            except (ValueError, TypeError):
                is_video = False
            if not is_video:
                meta['channel'] = 'activity'
        if event['role']=='assistant':
            meta['sender_id']='__agent__'
        payload = dict(meta, op='upsert',source_row=int(row_id),event_key=f'{self._agent_source}:message:{row_id}',
                       sender_name=event.get('sender_name','') or ('智能体' if event['role']=='assistant' else ''),role=event['role'],content=content,created_at=float(now))
        if meta['channel'] == 'video' and event['role'] == 'assistant':
            # Analysis is reusable external evidence, never a user's personal fact.
            payload['tier'] = 'long'
        if event['role']=='user':
            fingerprint = hashlib.sha256(re.sub(r'\s+','',content.strip()).encode()).hexdigest()
            row = self.connection.execute('SELECT tier FROM memories WHERE session_id=? AND sender_id=? AND fingerprint=?',
                                          (session,event.get('sender_id',''),fingerprint)).fetchone()
            if row:
                payload['tier'] = row[0]
        self.connection.execute('INSERT OR IGNORE INTO agent_outbox(event_key,payload) VALUES (?,?)',
                                (payload['event_key'],json.dumps(payload,ensure_ascii=False)))

    def backfill_agent_memory(self):
        if self.agent_memory is None:
            return 0
        with self._lock, self.connection:
            rows = self.connection.execute('''SELECT id,session_id,role,content,message_id,sender_id,sender_name,created_at
                FROM messages m WHERE NOT EXISTS (SELECT 1 FROM agent_outbox o WHERE o.event_key=? || m.id) ORDER BY id''',
                (self._agent_source+':message:',)).fetchall()
            if not rows:
                return 0
            payloads = {}
            for session, raw in self.connection.execute("SELECT session_id,payload FROM memory_inbox WHERE state='done'"):
                data = json.loads(raw)
                if data.get('message_id'):
                    payloads[(session,data['role'],data['message_id'])] = data
            before = self.connection.total_changes
            for row_id,session,role,content,message_id,sender,name,created in rows:
                event = dict(role=role,content=content,sender_id=sender,sender_name=name)
                original = payloads.get((session,role,message_id),{})
                if original.get('memory_content') is not None:
                    event['memory_content'] = original['memory_content']
                try:
                    stamp = datetime.fromisoformat(created).replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, TypeError):
                    stamp = time.time()
                self._queue_agent_event(row_id,session,event,stamp)
            return self.connection.total_changes-before

    def flush_agent_memory(self, limit=200, *, max_batches=None):
        if self.agent_memory is None:
            return 0
        processed = 0
        batches = 0
        with self._agent_flush_lock:
            while max_batches is None or batches < max_batches:
                with self._lock:
                    rows = self.connection.execute("SELECT id,payload FROM agent_outbox WHERE state='pending' ORDER BY id LIMIT ?",(limit,)).fetchall()
                if not rows:
                    break
                try:
                    self.agent_memory.publish([json.loads(raw) for _,raw in rows])
                except Exception as exc:
                    self._agent_last_error = type(exc).__name__
                    with self._lock,self.connection:
                        self.connection.executemany('UPDATE agent_outbox SET error=? WHERE id=?',[(self._agent_last_error,i) for i,_ in rows])
                    LOGGER.warning('共享记忆写入暂未完成；本地 outbox 已保留，错误类型=%s',self._agent_last_error)
                    break
                with self._lock,self.connection:
                    self.connection.executemany("UPDATE agent_outbox SET state='done',error='' WHERE id=?",[(i,) for i,_ in rows])
                processed += len(rows)
                batches += 1
                self._agent_last_error = ''
        return processed

    def _queue_agent_forget(self, session):
        if self.agent_memory is None:
            return
        maximum = self.connection.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?',(session,)).fetchone()[0]
        old = self.connection.execute("SELECT COALESCE(MAX(json_extract(payload,'$.upto_row_id')),0) FROM agent_outbox WHERE json_extract(payload,'$.source_session')=?",(session,)).fetchone()[0]
        self.connection.execute("DELETE FROM agent_outbox WHERE json_extract(payload,'$.source_session')=?",(session,))
        payload = dict(op='forget',source_id=self._agent_source,source_session=session,upto_row_id=max(maximum,old))
        self.connection.execute('INSERT INTO agent_outbox(event_key,payload) VALUES (?,?)',
                                ('forget:'+uuid.uuid4().hex,json.dumps(payload)))

    def _queue_agent_delivery(self, session, reply_id, state):
        if self.agent_memory is None:
            return
        row = self.connection.execute('SELECT id FROM messages WHERE session_id=? AND message_id=?',(session,reply_id)).fetchone()
        if not row:
            return
        target = f'{self._agent_source}:message:{row[0]}'
        payload = dict(op='delivery',target_key=target,state=state,source_session=session)
        self.connection.execute('INSERT OR IGNORE INTO agent_outbox(event_key,payload) VALUES (?,?)',
                                (target+':delivery:'+state,json.dumps(payload)))

    def _queue_agent_pin(self, memory_id):
        if self.agent_memory is None:
            return
        row=self.connection.execute('SELECT session_id,sender_id,fingerprint FROM memories WHERE id=?',(memory_id,)).fetchone()
        if not row:
            return
        payload=dict(self._agent_meta(row[0],row[1]),op='pin',fingerprint=row[2])
        self.connection.execute('INSERT INTO agent_outbox(event_key,payload) VALUES (?,?)',
                                ('pin:'+uuid.uuid4().hex,json.dumps(payload,ensure_ascii=False)))

    def memory_retriever(self, session, *, sender_id='', history=()):
        if self.agent_memory is None:
            return None
        self.flush_agent_memory()
        from .modules.memory_recall import MemoryRecallModule
        return MemoryRecallModule(self.agent_memory,current=self._agent_meta(session,sender_id),history=history)

    def current_agent_event_key(self,session,message_id,content):
        if self.agent_memory is None:
            return ''
        with self._lock:
            if message_id:
                row=self.connection.execute("SELECT id FROM messages WHERE session_id=? AND role='user' AND message_id=?",(session,message_id)).fetchone()
            else:
                row=self.connection.execute("SELECT id FROM messages WHERE session_id=? AND role='user' AND content=? ORDER BY id DESC LIMIT 1",(session,content)).fetchone()
        return f'{self._agent_source}:message:{row[0]}' if row else ''
