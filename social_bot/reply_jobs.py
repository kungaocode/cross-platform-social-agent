"""Durable model handoffs. Retry final generation without replaying tools."""
import json
import time
import uuid


class ReplyJobs:
    def init_reply_jobs(self):
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS reply_jobs(
            id TEXT PRIMARY KEY,session_id TEXT NOT NULL,message_id TEXT NOT NULL,
            incoming TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'processing',handoff TEXT,
            result TEXT,error TEXT NOT NULL DEFAULT '',render_token TEXT,lease REAL NOT NULL DEFAULT 0,
            updated REAL NOT NULL,UNIQUE(session_id,message_id));
          CREATE TABLE IF NOT EXISTS incoming_events(
            id TEXT PRIMARY KEY,session_id TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS reply_deliveries(job_id TEXT PRIMARY KEY,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
        ''')
        self.connection.commit()

    def start_reply_job(self,session_id,message_id,incoming):
        jid=uuid.uuid4().hex
        with self._lock,self.connection:
            self.connection.execute('INSERT OR IGNORE INTO reply_jobs(id,session_id,message_id,incoming,updated) VALUES(?,?,?,?,?)',
                (jid,session_id,message_id,json.dumps(incoming,ensure_ascii=False),time.time()))
        return self.reply_job(session_id=session_id,message_id=message_id)

    def reply_job(self,jid=None,*,session_id=None,message_id=None):
        with self._lock:
            cursor=self.connection.execute('SELECT * FROM reply_jobs WHERE id=?' if jid else
                'SELECT * FROM reply_jobs WHERE session_id=? AND message_id=?',
                (jid,) if jid else (session_id,message_id))
            row=cursor.fetchone()
            if row is None:return None
            result=dict(zip([c[0] for c in cursor.description],row))
        for field in ('incoming','handoff','result'):
            result[field]=json.loads(result[field]) if result[field] else None
        return result

    def checkpoint_reply_job(self,jid,phase,data):
        with self._lock,self.connection:
            self.connection.execute('UPDATE reply_jobs SET state=?,handoff=?,updated=? WHERE id=?',
                ('prepared' if phase=='prepared' else 'tool_recorded',json.dumps(data,ensure_ascii=False),time.time(),jid))

    def claim_reply_job(self,jid,seconds):
        token=uuid.uuid4().hex
        with self._lock,self.connection:
            changed=self.connection.execute("UPDATE reply_jobs SET state='rendering',render_token=?,lease=?,updated=? WHERE id=? AND (state IN ('prepared','final_failed') OR (state='rendering' AND lease<?))",
                (token,time.time()+seconds,time.time(),jid,time.time())).rowcount
        return token if changed else None

    def fail_reply_job(self,jid,error,token=None):
        with self._lock,self.connection:
            self.connection.execute("UPDATE reply_jobs SET state=CASE WHEN state='rendering' THEN 'final_failed' ELSE 'needs_review' END,error=?,lease=0,updated=? WHERE id=? AND (? IS NULL OR render_token=?)",
                (str(error)[:1000],time.time(),jid,token,token))

    def finish_reply_job(self,jid,result,token=None):
        job=self.reply_job(jid)
        reply_id='reply-job:'+jid
        with self._lock,self.connection:
            if token is not None:
                owner=self.connection.execute('SELECT render_token,state FROM reply_jobs WHERE id=?',(jid,)).fetchone()
                if owner != (token,'rendering'):return False
            # Commit the final reply and task completion in the same transaction.
            inserted=self.connection.execute('INSERT OR IGNORE INTO messages(session_id,role,content,message_id,sender_id,sender_name) VALUES(?,?,?,?,?,?)',
                (job['session_id'],'assistant',result['reply'],reply_id,'',''))
            if inserted.rowcount:
                self._queue_agent_event(inserted.lastrowid,job['session_id'],dict(role='assistant',content=result['reply']),time.time())
            self.connection.execute("UPDATE reply_jobs SET state='completed',result=?,error='',lease=0,updated=? WHERE id=?",
                (json.dumps(result,ensure_ascii=False),time.time(),jid))
            self.connection.execute("INSERT OR IGNORE INTO reply_deliveries(job_id,state,updated) VALUES(?,'ready',?)",(jid,time.time()))
        self.flush_agent_memory()
        return True

    def mark_reply_delivery(self,jid,state,error=''):
        if state not in ('sending','sent','failed','unknown'):raise ValueError('invalid delivery state')
        with self._lock,self.connection:
            if state=='sending':
                changed=self.connection.execute("UPDATE reply_deliveries SET state='sending',updated=? WHERE job_id=? AND state='ready'",(time.time(),jid)).rowcount==1
            else:
                self.connection.execute('UPDATE reply_deliveries SET state=?,error=?,updated=? WHERE job_id=?',(state,str(error)[:500],time.time(),jid))
                changed=True
            job=self.reply_job(jid)
            if changed and job:
                self._queue_agent_delivery(job['session_id'],'reply-job:'+jid,state)
        self.flush_agent_memory()
        return changed

    def stage_incoming(self,session_id,event_id,payload):
        with self._lock,self.connection:
            self.connection.execute('INSERT OR IGNORE INTO incoming_events(id,session_id,payload,updated) VALUES(?,?,?,?)',
                (event_id,session_id,json.dumps(payload,ensure_ascii=False),time.time()))

    def complete_incoming(self,event_id,state='done'):
        with self._lock,self.connection:
            self.connection.execute('UPDATE incoming_events SET state=?,updated=? WHERE id=?',(state,time.time(),event_id))

    def recover_incoming(self):
        with self._lock:
            rows=self.connection.execute("SELECT id,session_id,payload FROM incoming_events WHERE state='pending' ORDER BY updated").fetchall()
        for eid,session,payload in rows:
            data=json.loads(payload)
            self.add(session,'user',data['text'],data.get('message_id'),sender_id=data.get('sender_id',''),
                     sender_name=data.get('sender_name',''),memory_content=data.get('memory_text'))
            self.complete_incoming(eid,'archived_after_restart')
        return len(rows)
