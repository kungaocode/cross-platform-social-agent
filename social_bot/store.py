from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .memory import MemoryMixin
from .reply_jobs import ReplyJobs
from .agent_replica import AgentReplicaMixin


class ConversationStore(AgentReplicaMixin,MemoryMixin,ReplyJobs):
    """会话历史与群接通状态的 SQLite 存储。

    线程安全说明：sqlite3 的连接对象**不支持多线程同时使用**（会抛 InterfaceError /
    DatabaseError，实测 8 线程并发时 7 个线程报错）。而 wxauto 的监听回调运行在线程池里，
    多个群同时来消息就会并发调用到这里，因此所有数据库操作都必须经过同一把内部锁。
    """

    def __init__(self, database_path: str | Path, *, agent_memory_path=None, account_id='default', agent_retry_seconds=0) -> None:
        path = Path(database_path)
        self.database_path = path.expanduser().resolve()
        self._init_agent_replica()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
            DROP INDEX IF EXISTS idx_messages_message_id;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_scoped_id
                ON messages(session_id,role,message_id) WHERE message_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS group_states (
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(platform, group_id)
            );
            """
        )
        self.connection.commit()
        # 兼容既有数据库；旧记录没有可靠的发言人，保持为空，不推测归属。
        self.connection.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(messages)")}
        for column in ("sender_id", "sender_name"):
            if column not in columns:
                self.connection.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

        self.init_memory()
        self.init_reply_jobs()
        self.configure_agent_memory(agent_memory_path,account=account_id)
        self.start_agent_retry(agent_retry_seconds)

    @classmethod
    def from_runtime(cls, runtime, *, database_path=None, account_id=None):
        return cls(database_path or runtime.database_path,
                   agent_memory_path=getattr(runtime,'agent_memory_path','') if getattr(runtime,'agent_memory_enabled',False) else None,
                   account_id=account_id or getattr(runtime,'memory_account_id','default'),
                   agent_retry_seconds=getattr(runtime,'agent_memory_retry_seconds',30))

    def has_message(self, message_id: str | None, session_id: str | None = None) -> bool:
        if not message_id:
            return False
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM messages WHERE message_id = ? AND (? IS NULL OR session_id=?) LIMIT 1", (message_id, session_id, session_id)
            ).fetchone()
        return row is not None

    def add(self, session_id: str, role: str, content: str, message_id: str | None = None,
            *, sender_id: str = "", sender_name: str = "", memory_content: str | None = None) -> bool:
        """写入一条消息。message_id 重复时返回 False，而不是抛异常。

        唯一索引会挡住重复写入；并发去重存在竞态窗口，这里兜住它，
        避免把 IntegrityError 抛进 wxauto 的监听线程。
        """
        inserted = self.enqueue(session_id, role, content, message_id,
                                sender_id=sender_id, sender_name=sender_name, memory_content=memory_content)
        self.drain()
        return inserted

    def history(self, session_id: str, limit: int) -> list[dict[str, str]]:
        self.drain()
        with self._lock:
            rows = self.connection.execute(
                "SELECT role, content, sender_id, sender_name, created_at FROM messages WHERE session_id = ? AND created_at >= datetime('now','-7 days') ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": role, "content": (
            f"[发言人：{name or sender}；ID：{sender}；记录时间(UTC)：{created}]\n{content}"
            if role == "user" and ":group:" in session_id and (sender or name)
            else content
        )} for role, content, sender, name, created in reversed(rows)]

    def clear(self, session_id: str) -> None:
        with self._agent_flush_lock,self._lock,self.connection:
            self._queue_agent_forget(session_id)
            self.connection.execute('DELETE FROM reply_deliveries WHERE job_id IN (SELECT id FROM reply_jobs WHERE session_id=?)',(session_id,))
            self.connection.execute('DELETE FROM reply_jobs WHERE session_id=?',(session_id,))
            self.connection.execute('DELETE FROM incoming_events WHERE session_id=?',(session_id,))
            self.connection.execute('DELETE FROM memory_terms WHERE memory_id IN (SELECT id FROM memories WHERE session_id=?)',(session_id,))
            self.connection.execute("DELETE FROM memory_sources WHERE memory_id IN (SELECT id FROM memories WHERE session_id=?)", (session_id,))
            self.connection.execute("DELETE FROM memories WHERE session_id=?", (session_id,))
            self.connection.execute("DELETE FROM memory_inbox WHERE session_id=?", (session_id,))
            self.connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.connection.commit()
        self.flush_agent_memory()

    def group_active(self, platform: str, group_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT active FROM group_states WHERE platform = ? AND group_id = ?",
                (platform, group_id),
            ).fetchone()
        return bool(row and row[0])

    def set_group_active(self, platform: str, group_id: str, active: bool) -> None:
        with self._lock:
            self.connection.execute(
                """INSERT INTO group_states(platform, group_id, active, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(platform, group_id) DO UPDATE SET
                     active = excluded.active, updated_at = CURRENT_TIMESTAMP""",
                (platform, group_id, int(active)),
            )
            self.connection.commit()

    def close(self) -> None:
        self.stop_agent_retry()
        self.flush_agent_memory()
        with self._lock:
            if self.agent_memory is not None:
                self.agent_memory.close()
            self.connection.close()
