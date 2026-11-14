"""消息处理和聊天窗输出的并发辅助工具。"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ChatOutputTask:
    """一次待发送操作；任务中始终保留目标聊天窗信息。"""

    chat_key: str
    chat: Any
    action: Callable[[], Any]
    label: str = "send"
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class ChatOutputQueue:
    """用一个全局发送队列串行执行所有聊天窗的输出操作。

    微信桌面客户端的发送通道是全局单通道，不同聊天窗也不能同时调用发送接口。
    每个任务仍保留 ``chat_key`` 和 ``chat``，出队时使用任务自带的目标聊天窗，
    防止多个聊天窗之间串线。不额外创建输出线程：提交任务的消息处理线程在需要时
    负责排空全局队列，因此总线程数仍由消息处理池控制。
    """

    def __init__(self, key_fn: Callable[[Any], str], max_pending_per_chat: int = 100) -> None:
        self._key_fn = key_fn
        self._max_pending = max(1, int(max_pending_per_chat))
        self._lock = threading.RLock()
        self._queue: collections.deque[ChatOutputTask] = collections.deque()
        self._active = False
        self._closed = False

    def enqueue(self, chat: Any, action: Callable[[], Any], *, label: str = "send") -> Any:
        """将发送动作入队并等待出队执行，返回动作结果。"""
        chat_key = str(self._key_fn(chat) or "").strip() or f"chat:{id(chat)}"
        task = ChatOutputTask(chat_key=chat_key, chat=chat, action=action, label=label)
        with self._lock:
            if self._closed:
                raise RuntimeError("chat_output_queue_closed")
            if len(self._queue) >= self._max_pending:
                raise RuntimeError("chat_output_queue_full")
            self._queue.append(task)
            should_drain = not self._active
            if should_drain:
                self._active = True

        if should_drain:
            self._drain()
        task.done.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._active = False
                    return
                task = self._queue.popleft()
            try:
                task.result = task.action()
            except BaseException as exc:  # noqa: BLE001 - 传回调用方，不让队列卡死
                task.error = exc
            finally:
                task.done.set()

    def pending(self, chat: Any) -> int:
        """返回指定聊天窗当前等待执行的任务数。"""
        chat_key = str(self._key_fn(chat) or "").strip() or f"chat:{id(chat)}"
        with self._lock:
            return sum(1 for task in self._queue if task.chat_key == chat_key)

    def close(self) -> None:
        """阻止新的输出；已入队任务仍由当前排空者处理。"""
        with self._lock:
            self._closed = True
