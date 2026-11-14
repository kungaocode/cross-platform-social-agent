"""并发回归测试。

背景（都是实测踩出来的）：
  1. sqlite3 连接不能多线程同时用，而 wxauto 回调跑在线程池里 → 多群同时来消息会崩。
  2. BotService 原先用一把全局锁把模型调用也包住 → 多群实际是串行的，一个群卡住全都卡住。
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from social_bot.service import BotService
from social_bot.dispatch import ChatOutputQueue
from social_bot.settings import (
    AppSettings,
    DeepSeekSettings,
    RuntimeSettings,
    ScopeSettings,
    WeChatSettings,
)
from social_bot.store import ConversationStore


def _settings(folder: str, groups: tuple[str, ...], cooldown: int = 0) -> AppSettings:
    return AppSettings(
        api_key="unused",
        deepseek=DeepSeekSettings(),
        scope=ScopeSettings(groups=frozenset(groups), group_require_mention=True),
        runtime=RuntimeSettings(database_path=str(Path(folder) / "bot.sqlite3"), cooldown_seconds=cooldown),
        wechat=WeChatSettings(enabled=True, targets=groups, bot_name="大肥鱼"),
    )


def _group_message(group: str, text: str = "@大肥鱼 你好", message_id: str = "") -> dict:
    return {
        "platform": "wechat",
        "chat_type": "group",
        "chat_id": group,
        "sender_id": "某人",
        "text": text,
        "message_id": message_id or f"m-{group}-{time.monotonic_ns()}",
        "is_mentioned": True,
    }


class StoreConcurrencyTests(unittest.TestCase):
    """回归：Store 必须能在多线程下安全使用。"""

    def test_concurrent_access_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "t.sqlite3")
            errors: list[str] = []
            per_thread = 120

            def worker(tid: int) -> None:
                try:
                    for i in range(per_thread):
                        store.add(f"s{tid}", "user", f"c{tid}-{i}", f"mid-{tid}-{i}")
                        store.has_message(f"mid-{tid}-{i}")
                        store.group_active("wechat", f"g{tid}")
                        store.set_group_active("wechat", f"g{tid}", True)
                        store.history(f"s{tid}", 5)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"thread{tid}: {type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"并发访问 Store 报错：{errors[:3]}")
            rows = store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(rows, 8 * per_thread)
            store.close()

    def test_duplicate_message_id_returns_false_instead_of_raising(self) -> None:
        """唯一索引撞车时不能抛异常，否则会窜进 wxauto 的监听线程。"""
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "t.sqlite3")
            self.assertTrue(store.add("s1", "user", "第一条", "same-id"))
            self.assertFalse(store.add("s1", "user", "重复的", "same-id"))
            store.close()


class ParallelClient:
    """记录模型调用的峰值并发数。"""

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def complete(self, messages):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.delay)
        with self._lock:
            self.running -= 1
        return "回复"


class BarrierClient:
    """必须所有调用者同时到达才放行；若被串行化则超时失败。"""

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=5)
        self.broken: str | None = None

    def complete(self, messages):
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            self.broken = "模型调用没有并行，被串行化了"
            raise
        return "回复"


class ServiceConcurrencyTests(unittest.TestCase):
    """回归：不同群应当并行，同一个群仍然串行。"""

    def test_different_groups_run_in_parallel(self) -> None:
        groups = ("群A", "群B", "群C")
        with tempfile.TemporaryDirectory() as folder:
            service = BotService(_settings(folder, groups), client=BarrierClient(len(groups)))
            for g in groups:
                service.store.set_group_active("wechat", g, True)

            results: dict[str, dict] = {}
            threads = [
                threading.Thread(target=lambda g=g: results.__setitem__(g, service.handle(_group_message(g))))
                for g in groups
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertIsNone(getattr(service.client, "broken", None))
            self.assertEqual(sorted(results), sorted(groups))
            for g in groups:
                self.assertTrue(results[g]["accepted"], f"{g} 未被接受: {results[g]}")
            service.close()

    def test_same_group_stays_serialized(self) -> None:
        """同一个群必须串行，否则冷却判断和上下文顺序都会乱。"""
        with tempfile.TemporaryDirectory() as folder:
            client = ParallelClient(delay=0.15)
            service = BotService(_settings(folder, ("群A",)), client=client)
            service.store.set_group_active("wechat", "群A", True)

            threads = [
                threading.Thread(target=service.handle, args=(_group_message("群A", message_id=f"m{i}"),))
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(client.peak, 1, "同一个群出现了并行模型调用")
            service.close()


class ChatOutputQueueTests(unittest.TestCase):
    def test_same_chat_output_is_serialized_and_keeps_order(self) -> None:
        chat = object()
        queue = ChatOutputQueue(lambda value: "same" if value is chat else "other")
        running = 0
        peak = 0
        lock = threading.Lock()
        order: list[int] = []

        def send(index: int) -> str:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                order.append(index)
            time.sleep(0.03)
            with lock:
                running -= 1
            return str(index)

        threads = [threading.Thread(target=lambda i=i: queue.enqueue(chat, lambda i=i: send(i))) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(peak, 1)
        self.assertEqual(sorted(order), [0, 1, 2, 3])
        queue.close()

    def test_different_chats_share_one_global_output_channel(self) -> None:
        queue = ChatOutputQueue(lambda value: value)
        first, second = object(), object()
        running = 0
        peak = 0
        lock = threading.Lock()

        def send(chat: object) -> None:
            nonlocal running, peak
            def action() -> None:
                nonlocal running, peak
                with lock:
                    running += 1
                    peak = max(peak, running)
                time.sleep(0.03)
                with lock:
                    running -= 1
            queue.enqueue(chat, action)

        threads = [threading.Thread(target=send, args=(chat,)) for chat in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(peak, 1)
        queue.close()


if __name__ == "__main__":
    unittest.main()
