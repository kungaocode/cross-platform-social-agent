"""One agent's cross-platform episodic memory, with provenance and explicit identity links."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .memory import classify_statement, tokens, strength


def endpoint(platform, account, sender):
    return json.dumps([platform, account, sender], ensure_ascii=False, separators=(',', ':'))


class AgentMemory:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._db = None

    def _connect(self):
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA busy_timeout=10000')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_events(
                  id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL,
                  source_row INTEGER NOT NULL, source_session TEXT NOT NULL, platform TEXT NOT NULL,
                  account TEXT NOT NULL, channel TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                  endpoint TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  tier TEXT NOT NULL, importance REAL NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_agent_events_time ON agent_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_events_source ON agent_events(source_id,source_session,source_row);
                CREATE TABLE IF NOT EXISTS agent_terms(event_id INTEGER NOT NULL,token TEXT NOT NULL,PRIMARY KEY(event_id,token));
                CREATE INDEX IF NOT EXISTS idx_agent_terms ON agent_terms(token,event_id);
                CREATE TABLE IF NOT EXISTS agent_identities(endpoint TEXT PRIMARY KEY,person_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_forgets(source_id TEXT NOT NULL,source_session TEXT NOT NULL,
                  upto_row_id INTEGER NOT NULL,PRIMARY KEY(source_id,source_session));
                CREATE TABLE IF NOT EXISTS agent_deliveries(event_key TEXT PRIMARY KEY,state TEXT NOT NULL);
            ''')
            db.commit()
        except BaseException:
            db.close()
            raise
        self._db = db
        return db

    @staticmethod
    def clean_content(value):
        text = str(value or '')
        text = re.sub(r'data:image/[^\s,]+;base64,[A-Za-z0-9+/=]+|base64://[A-Za-z0-9+/=]+', '[图片数据省略]', text)
        return text[:16000]

    def publish(self, payloads):
        with self._lock:
            db = self._connect()
            with db:
                for event in payloads:
                    op = event.get('op', 'upsert')
                    if op == 'forget':
                        args = (event['source_id'], event['source_session'], int(event['upto_row_id']))
                        db.execute('''INSERT INTO agent_forgets VALUES(?,?,?) ON CONFLICT(source_id,source_session)
                                   DO UPDATE SET upto_row_id=MAX(upto_row_id,excluded.upto_row_id)''', args)
                        db.execute('DELETE FROM agent_terms WHERE event_id IN (SELECT id FROM agent_events WHERE source_id=? AND source_session=? AND source_row<=?)', args)
                        db.execute('DELETE FROM agent_deliveries WHERE event_key IN (SELECT event_key FROM agent_events WHERE source_id=? AND source_session=? AND source_row<=?)', args)
                        db.execute('DELETE FROM agent_events WHERE source_id=? AND source_session=? AND source_row<=?', args)
                        continue
                    if op == 'delivery':
                        db.execute('INSERT INTO agent_deliveries VALUES (?,?) ON CONFLICT(event_key) DO UPDATE SET state=excluded.state',
                                   (event['target_key'], event['state']))
                        continue
                    if op == 'pin':
                        db.execute("UPDATE agent_events SET tier='core',importance=1 WHERE source_id=? AND source_session=? AND sender_id=? AND fingerprint=? AND role='user'",
                                   (event['source_id'],event['source_session'],event['sender_id'],event['fingerprint']))
                        continue
                    old = db.execute('SELECT upto_row_id FROM agent_forgets WHERE source_id=? AND source_session=?',
                                     (event['source_id'], event['source_session'])).fetchone()
                    if old and event['source_row'] <= old[0]:
                        continue
                    content = self.clean_content(event['content'])
                    tier, importance = classify_statement(content) if event['role'] == 'user' else ('recent', .2)
                    if event.get('tier') in {'core', 'long'}:
                        tier = event['tier']
                    if tier=='core':
                        importance=1.0
                    fingerprint = hashlib.sha256(re.sub(r'\s+', '', content).encode()).hexdigest()
                    identity = endpoint(event['platform'], event['account'], event['sender_id'])
                    cursor = db.execute('''INSERT OR IGNORE INTO agent_events(event_key,source_id,source_row,source_session,platform,
                        account,channel,sender_id,sender_name,endpoint,role,content,fingerprint,tier,importance,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        tuple(event[k] for k in ('event_key','source_id','source_row','source_session','platform','account','channel','sender_id','sender_name'))
                        + (identity,event['role'],content,fingerprint,tier,importance,event['created_at']))
                    if cursor.rowcount:
                        db.executemany('INSERT OR IGNORE INTO agent_terms VALUES (?,?)', [(cursor.lastrowid, token) for token in tokens(content)])

    def link_identity(self, platform, account, sender, person):
        if not all(isinstance(x, str) and x.strip() for x in (platform, account, sender, person)):
            raise ValueError('身份关联参数不能为空')
        with self._lock:
            db = self._connect()
            with db:
                db.execute('INSERT INTO agent_identities VALUES (?,?) ON CONFLICT(endpoint) DO UPDATE SET person_id=excluded.person_id',
                           (endpoint(platform, account, sender), person))

    def unlink_identity(self, platform, account, sender):
        with self._lock:
            db = self._connect()
            with db:
                db.execute('DELETE FROM agent_identities WHERE endpoint=?', (endpoint(platform, account, sender),))

    def identities(self):
        with self._lock:
            return [dict(endpoint=json.loads(row[0]), person_id=row[1]) for row in self._connect().execute('SELECT endpoint,person_id FROM agent_identities ORDER BY person_id,endpoint')]

    @staticmethod
    def _source(row):
        source = dict(platform=row['platform'], account=row['account'], session=row['source_session'], channel=row['channel'],
                    sender_id=row['sender_id'], sender_name=row['sender_name'], identity=row['person'],
                    time=datetime.fromtimestamp(row['created_at'], timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(),
                    role=row['role'], delivery=row['delivery'] or ('generated' if row['role'] == 'assistant' else 'received'))
        if row['channel'] == 'video' and row['role'] == 'assistant':
            try:
                evidence = json.loads(row['content'])
                source.update(url=str(evidence.get('url', ''))[:300],
                              title=str(evidence.get('title', ''))[:100],
                              coverage_start_seconds=evidence.get('coverage_start_seconds'),
                              coverage_end_seconds=evidence.get('coverage_end_seconds'),
                              analysis_verified=evidence.get('analysis_verified'),
                              review_status=evidence.get('review_status'),
                              has_audio=evidence.get('has_audio'),
                              transcript_source=evidence.get('transcript_source'))
            except (ValueError, TypeError, AttributeError):
                pass
        return source

    def _rows(self, where, args, limit, query_tokens=()):
        db = self._connect()
        ranking = ''
        if query_tokens:
            ranking = '(SELECT COUNT(*) FROM agent_terms t WHERE t.event_id=e.id AND t.token IN (' + ','.join('?' for _ in query_tokens) + ')) DESC,'
        cursor = db.execute('''SELECT e.*,COALESCE(i.person_id,e.endpoint) AS person,d.state AS delivery
                FROM agent_events e LEFT JOIN agent_identities i ON i.endpoint=e.endpoint
                LEFT JOIN agent_deliveries d ON d.event_key=e.event_key
                WHERE ''' + where + ' ORDER BY ' + ranking + " CASE e.tier WHEN 'core' THEN 2 WHEN 'long' THEN 1 ELSE 0 END DESC,e.created_at DESC,e.id DESC LIMIT ?",
                [*args, *query_tokens, limit])
        return [dict(zip([col[0] for col in cursor.description], row)) for row in cursor]

    def recall(self, query, *, current=None, limit=8, max_chars=5200, recent_limit=0, profile=False, eligibility=None):
        """Search all platforms; public replies can use video evidence, public comments and their own thread."""
        current = current or {}
        qt = tokens(str(query))
        selected = sorted(qt)[:128]
        where, args = '1=1', []
        if current.get('exclude_event_key'):
            where+=' AND e.event_key<>?'
            args.append(current['exclude_event_key'])
        if current.get('channel') in {'group', 'comment'}:
            where += " AND (e.channel IN ('comment','video') OR (e.source_id=? AND e.source_session=?))"
            args.extend([current.get('source_id', ''), current.get('source_session', '')])
        match = '0'
        if selected:
            match += ' OR e.id IN (SELECT event_id FROM agent_terms WHERE token IN (' + ','.join('?' for _ in selected) + '))'
        with self._lock:
            identity = endpoint(current.get('platform',''), current.get('account',''), current.get('sender_id',''))
            linked = self._connect().execute('SELECT person_id FROM agent_identities WHERE endpoint=?', (identity,)).fetchone()
            person = linked[0] if linked else identity
            if profile:
                # Personal questions use this endpoint or explicitly linked accounts only.
                # Apply before the candidate limit so other people's matches cannot crowd it out.
                where += " AND e.role='user' AND COALESCE(i.person_id,e.endpoint)=?"
                args.append(person)
            match_args = [*args, *selected]
            if profile:
                match += " OR (e.role='user' AND COALESCE(i.person_id,e.endpoint)=? AND e.tier IN ('core','long'))"
                match_args.append(person)
            rows = self._rows(where + ' AND (' + match + ')', match_args, 2000, selected)
            recent = self._rows(where + ' AND NOT (e.source_id=? AND e.source_session=?)',
                                [*args, current.get('source_id',''), current.get('source_session','')], max(0, min(12, recent_limit)))
            universe_size = self._connect().execute('SELECT COUNT(*) FROM agent_events').fetchone()[0]
        groups = {}
        now = time.time()
        for row in rows:
            if not row['content'].strip() or (row['content'].startswith('[') and row['content'].endswith(']')):
                continue
            key = (row['person'] if row['role'] == 'user' else 'agent', row['role'], row['fingerprint'])
            groups.setdefault(key, []).append(row)
        ranked = []
        for values in groups.values():
            values.sort(key=lambda row:(row['created_at'],row['id']),reverse=True)
            row = values[0]
            overlap = len(tokens(row['content']) & qt) / max(1, len(qt))
            tier = 'core' if any(r['tier'] == 'core' for r in values) else 'long' if len(values) >= 3 or any(r['tier'] == 'long' for r in values) else 'recent'
            if not overlap and not (profile and row['person']==person):
                continue
            kind='user_statement' if row['role']=='user' else 'assistant_generated'
            if row['channel'] == 'video' and row['role'] == 'assistant':
                kind = 'video_observation'
            if row['role']=='user' and re.search(r'[?？]\s*$|(?:吗|呢|么)[。！!]?$',row['content']):
                kind='user_question'
            if eligibility is not None and not eligibility(row['content'],self._source(row),tier,kind):
                continue
            score = strength(tier, max(r['importance'] for r in values), len(values), (now-row['created_at'])/86400) * (1+3*overlap)
            score *= 2 if row['person'] == person else 1
            if score < .05:
                continue
            item = dict(id=row['id'], content=row['content'], tier=tier, repeats=len(values), score=round(score,4),
                        age_days=max(0,(now-row['created_at'])/86400),
                        kind=kind,
                        same_person=row['role']=='user' and row['person']==person,
                        sources=[self._source(r) for r in values[:3]])
            ranked.append((score,row['id'],item))
        facts, used = [], 0
        for _, _, item in sorted(ranked, key=lambda r: (-r[0],-r[1])):
            size = len(json.dumps(item,ensure_ascii=False))
            if used + size <= max_chars and len(facts) < limit:
                facts.append(item)
                used += size
        episodes = []
        for row in reversed(recent):
            item = dict(content=row['content'][:1600], source=self._source(row))
            size = len(json.dumps(item,ensure_ascii=False))
            if used + size > max_chars + 3600:
                continue
            episodes.append(item)
            used += size
        return dict(memories=facts, recent_chain=episodes, current_identity=person,
                    candidates_examined=len(rows),universe_size=universe_size)

    def stats(self):
        with self._lock:
            db = self._connect()
            return dict(events=db.execute('SELECT COUNT(*) FROM agent_events').fetchone()[0],
                        platforms=dict(db.execute('SELECT platform,COUNT(*) FROM agent_events GROUP BY platform')),
                        tiers=dict(db.execute('SELECT tier,COUNT(*) FROM agent_events GROUP BY tier')),
                        identities=db.execute('SELECT COUNT(*) FROM agent_identities').fetchone()[0],
                        integrity=db.execute('PRAGMA quick_check').fetchone()[0])

    def pin(self, event_id):
        with self._lock:
            db = self._connect()
            with db:
                return db.execute("UPDATE agent_events SET tier='core',importance=1 WHERE id=? AND role='user'",(event_id,)).rowcount

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
