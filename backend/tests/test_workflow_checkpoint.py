"""
MySQL Checkpointer 测试（Phase F）
    覆盖：
        - 检查点写入/读取/最新检查点；
        - 通道值按版本回填；
        - pending writes 写入与排序；
        - list 的 before/filter/limit；
        - 删除线程；
        - 版本号生成。
    约束：使用测试库，不联网、不调 LLM。
"""
import unittest
from datetime import datetime, timezone

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.workflow.checkpoint import MySQLCheckpointSaver
from tests.support import BaseTestCase



# 构造检查点
def make_checkpoint(checkpoint_id: str, values: dict | None = None) -> dict:
    """
    构造一个最小可用的 LangGraph 检查点
        :param checkpoint_id: 检查点ID
        :param values: 通道值
        :return: 检查点字典
    """
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel_values": values or {"messages": ["hello"]},
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "updated_channels": ["messages"],
    }



class MySQLCheckpointSaverTest(BaseTestCase, unittest.TestCase):
    """检查点持久化"""

    def setUp(self):
        """初始化测试库与 saver"""
        super().setUp()
        self.saver = MySQLCheckpointSaver(session_factory=self.Session)

    def test_put_and_get_latest(self):
        """写入后能读回最新检查点与通道值"""
        config = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": ""}}
        checkpoint = make_checkpoint("00000000000000000000000000000001.0.0000000000000001")
        metadata = {"source": "loop", "step": 1, "parents": {}, "run_id": "run-1"}

        saved = self.saver.put(config, checkpoint, metadata, {"messages": "1"})

        self.assertEqual(saved["configurable"]["checkpoint_id"], checkpoint["id"])
        loaded = self.saver.get_tuple({"configurable": {"thread_id": "thread-a"}})
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.checkpoint["channel_values"]["messages"], ["hello"])
        self.assertEqual(loaded.metadata["source"], "loop")
        self.assertEqual(loaded.config["configurable"]["checkpoint_id"], checkpoint["id"])

    def test_put_writes_and_pending(self):
        """pending writes 能按任务顺序读回"""
        config = {"configurable": {"thread_id": "thread-b", "checkpoint_ns": ""}}
        checkpoint = make_checkpoint("00000000000000000000000000000001.0.0000000000000001")
        saved = self.saver.put(config, checkpoint, {"source": "loop", "step": 1}, {"messages": "1"})
        self.saver.put_writes(saved, [("messages", ["world"])], "task-1", "task-path")

        loaded = self.saver.get_tuple({"configurable": {"thread_id": "thread-b"}})
        self.assertEqual(loaded.pending_writes, [("task-1", "messages", ["world"])])

    def test_list_before_filter_limit(self):
        """list 支持 before、metadata 过滤与 limit"""
        config = {"configurable": {"thread_id": "thread-c", "checkpoint_ns": ""}}
        first = "00000000000000000000000000000001.0.0000000000000001"
        second = "00000000000000000000000000000002.0.0000000000000002"
        self.saver.put(config, make_checkpoint(first), {"source": "loop", "step": 1}, {"messages": "1"})
        self.saver.put(config, make_checkpoint(second), {"source": "update", "step": 2}, {"messages": "2"})

        all_items = list(self.saver.list({"configurable": {"thread_id": "thread-c"}}))
        self.assertEqual([item.checkpoint["id"] for item in all_items], [second, first])

        loop_items = list(
            self.saver.list(
                {"configurable": {"thread_id": "thread-c"}},
                filter={"source": "loop"},
            )
        )
        self.assertEqual(len(loop_items), 1)
        self.assertEqual(loop_items[0].checkpoint["id"], first)

        before_items = list(
            self.saver.list(
                {"configurable": {"thread_id": "thread-c"}},
                before={"configurable": {"thread_id": "thread-c", "checkpoint_id": second}},
            )
        )
        self.assertEqual([item.checkpoint["id"] for item in before_items], [first])

        limited = list(
            self.saver.list({"configurable": {"thread_id": "thread-c"}}, limit=1)
        )
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0].checkpoint["id"], second)

    def test_delete_thread(self):
        """删除线程后读不到任何检查点"""
        config = {"configurable": {"thread_id": "thread-d", "checkpoint_ns": ""}}
        self.saver.put(
            config,
            make_checkpoint("00000000000000000000000000000001.0.0000000000000001"),
            {"source": "loop", "step": 1},
            {"messages": "1"},
        )
        self.saver.delete_thread("thread-d")
        self.assertIsNone(self.saver.get_tuple({"configurable": {"thread_id": "thread-d"}}))

    def test_get_next_version(self):
        """版本号按递增序号生成"""
        first = self.saver.get_next_version(None, None)
        second = self.saver.get_next_version(first, None)
        self.assertEqual(int(first.split(".")[0]), 1)
        self.assertEqual(int(second.split(".")[0]), 2)
