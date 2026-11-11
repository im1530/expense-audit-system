"""
MySQL Checkpointer（Phase F：LangGraph 检查点持久化）
    背景：
        - LangGraph 默认 MemorySaver 只存在进程内存，进程重启/worker 重建后检查点全部丢失；
        - 本项目只跑 MySQL，不引入 SQLite/Postgres 依赖，因此实现一个 MySQL 版 Checkpointer。
    实现方式：
        - 继承 BaseCheckpointSaver，把 InMemorySaver 的三块内存结构落到三张表：
            storage  → workflow_checkpoints
            blobs    → workflow_checkpoint_blobs
            writes   → workflow_checkpoint_writes
        - 序列化复用 LangGraph 的 JsonPlusSerializer（默认支持 dataclass/Decimal/date 等）；
        - 每次读写使用独立 Session，避免与请求/工作流会话互相影响。
    边界：
        - 只做持久化，不改变图结构、节点逻辑与决策；
        - 节点级重试仍由 LangGraph/engine 负责，本模块只保证检查点可恢复；
        - 大字段用 LONGBLOB，禁止把原始发票图片写入检查点（状态里只有脱敏上下文）。
"""
import logging
import random
from collections.abc import Iterator, Sequence
from typing import Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.dependencies import SessionLocal
from app.models import (
    WorkflowCheckpoint,
    WorkflowCheckpointBlob,
    WorkflowCheckpointWrite,
)



logger = logging.getLogger(__name__)



# 写入排序：与 LangGraph 内部 writes_sort_key 保持一致，保证 reducer 重放顺序正确
def _writes_sort_key(row: WorkflowCheckpointWrite) -> tuple[str, str, int]:
    """
    生成 pending write 的排序键
        :param row: 写入记录
        :return: (任务路径, 任务ID, 序号)
    """
    return (row.task_path or "", row.task_id, int(row.idx))



# 反序列化白名单：检查点里会出现业务 dataclass，必须显式登记，避免未来版本直接拒绝
ALLOWED_MSGPACK_MODULES = (
    ("app.rag.schema", "PolicyBundle"),
    ("app.rag.schema", "PolicyHit"),
    ("app.rag.schema", "PolicyClause"),
    ("app.agents.risk_agent", "ReviewOutcome"),
)



# 构建带业务白名单的序列化器
def build_serde() -> JsonPlusSerializer:
    """
    构建检查点序列化器
        :return: 允许业务 dataclass 的 JsonPlusSerializer
    """
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES)



# MySQL 版检查点存储
class MySQLCheckpointSaver(BaseCheckpointSaver[str]):
    """
    LangGraph 检查点存储（MySQL）
        :ivar session_factory: 会话工厂；默认使用全局 SessionLocal
    """

    def __init__(self, *, session_factory=None, serde=None):
        """
        :param session_factory: 数据库会话工厂（测试可注入）
        :param serde: 序列化器；默认由父类创建 JsonPlusSerializer
        """
        super().__init__(serde=serde or build_serde())       # 初始化序列化器（带业务白名单）
        self.session_factory = session_factory or SessionLocal   # 每次操作独立会话

    # 读取单个检查点
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """
        读取一个检查点
            :param config: 含 thread_id / checkpoint_ns / checkpoint_id 的配置
            :return: CheckpointTuple；不存在返回 None
        """
        thread_id = config["configurable"]["thread_id"]                 # 线程ID
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") # 命名空间
        checkpoint_id = get_checkpoint_id(config)                       # 指定检查点ID

        db = self.session_factory()
        try:
            stmt = select(WorkflowCheckpoint).where(
                WorkflowCheckpoint.thread_id == thread_id,
                WorkflowCheckpoint.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id:
                stmt = stmt.where(WorkflowCheckpoint.checkpoint_id == checkpoint_id)
            else:
                stmt = stmt.order_by(WorkflowCheckpoint.checkpoint_id.desc()).limit(1)
            row = db.execute(stmt).scalars().first()
            if row is None:
                return None
            return self._to_tuple(db, row, config if checkpoint_id else None)
        finally:
            db.close()

    # 列出检查点
    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """
        按条件列出检查点（按 checkpoint_id 倒序）
            :param config: 线程/命名空间过滤
            :param filter: metadata 等值过滤
            :param before: 只返回该检查点之前的记录
            :param limit: 最大条数
            :return: CheckpointTuple 迭代器
        """
        db = self.session_factory()
        try:
            stmt = select(WorkflowCheckpoint).order_by(WorkflowCheckpoint.checkpoint_id.desc())
            if config:
                stmt = stmt.where(
                    WorkflowCheckpoint.thread_id == config["configurable"]["thread_id"],
                )
                checkpoint_ns = config["configurable"].get("checkpoint_ns")
                if checkpoint_ns is not None:
                    stmt = stmt.where(WorkflowCheckpoint.checkpoint_ns == checkpoint_ns)
            before_id = get_checkpoint_id(before) if before else None
            if before_id:
                stmt = stmt.where(WorkflowCheckpoint.checkpoint_id < before_id)

            emitted = 0
            for row in db.execute(stmt).scalars():
                metadata = self.serde.loads_typed((row.metadata_type, row.metadata_blob))
                if filter and not all(metadata.get(k) == v for k, v in filter.items()):
                    continue
                if limit is not None and emitted >= limit:
                    break
                emitted += 1
                yield self._to_tuple(db, row, None)
        finally:
            db.close()

    # 写入检查点
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """
        保存检查点与新增通道值
            :param config: 当前配置（含父检查点ID）
            :param checkpoint: 检查点对象
            :param metadata: 检查点元数据
            :param new_versions: 本轮新增/变化的通道版本
            :return: 指向新检查点的配置
        """
        thread_id = config["configurable"]["thread_id"]                 # 线程ID
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") # 命名空间

        # 1.拆出 channel_values，单独写 blob 表
        body = checkpoint.copy()                                        # 浅拷贝，避免修改调用方对象
        values: dict[str, Any] = body.pop("channel_values")             # 通道值

        db = self.session_factory()
        try:
            # 2.新版本通道逐个落库；本轮没有值的通道写 empty 占位
            for channel, version in new_versions.items():
                type_, blob = (
                    self.serde.dumps_typed(values[channel])
                    if channel in values
                    else ("empty", b"")
                )
                self._upsert(
                    db,
                    WorkflowCheckpointBlob.__table__,
                    {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "channel": channel,
                        "version": str(version),
                        "type": type_,
                        "blob": blob,
                    },
                    ("type", "blob"),
                )

            # 3.检查点主体与元数据落库
            checkpoint_type, checkpoint_blob = self.serde.dumps_typed(body)
            metadata_type, metadata_blob = self.serde.dumps_typed(
                get_checkpoint_metadata(config, metadata)
            )
            self._upsert(
                db,
                WorkflowCheckpoint.__table__,
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint["id"],
                    "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
                    "checkpoint_type": checkpoint_type,
                    "checkpoint_blob": checkpoint_blob,
                    "metadata_type": metadata_type,
                    "metadata_blob": metadata_blob,
                },
                ("parent_checkpoint_id", "checkpoint_type", "checkpoint_blob", "metadata_type", "metadata_blob"),
            )
            db.commit()
        finally:
            db.close()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    # 写入节点产生的 pending writes
    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """
        保存节点写入
            :param config: 当前配置（必须含 checkpoint_id）
            :param writes: [(channel, value), ...]
            :param task_id: 任务ID
            :param task_path: 任务路径
        """
        thread_id = config["configurable"]["thread_id"]                 # 线程ID
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") # 命名空间
        checkpoint_id = config["configurable"]["checkpoint_id"]         # 检查点ID

        db = self.session_factory()
        try:
            for raw_idx, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, raw_idx)               # 特殊通道使用负数序号
                # 正序号重复写入直接跳过；负序号（错误/中断等）允许覆盖
                if idx >= 0:
                    exists = db.execute(
                        select(WorkflowCheckpointWrite.idx).where(
                            WorkflowCheckpointWrite.thread_id == thread_id,
                            WorkflowCheckpointWrite.checkpoint_ns == checkpoint_ns,
                            WorkflowCheckpointWrite.checkpoint_id == checkpoint_id,
                            WorkflowCheckpointWrite.task_id == task_id,
                            WorkflowCheckpointWrite.idx == idx,
                        )
                    ).first()
                    if exists is not None:
                        continue

                type_, blob = self.serde.dumps_typed(value)
                self._upsert(
                    db,
                    WorkflowCheckpointWrite.__table__,
                    {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                        "task_id": task_id,
                        "idx": idx,
                        "channel": channel,
                        "type": type_,
                        "blob": blob,
                        "task_path": task_path,
                    },
                    ("channel", "type", "blob", "task_path"),
                )
            db.commit()
        finally:
            db.close()

    # 删除线程的全部检查点
    def delete_thread(self, thread_id: str) -> None:
        """
        删除一个线程的所有检查点、通道值与写入
            :param thread_id: 线程ID
        """
        db = self.session_factory()
        try:
            db.execute(delete(WorkflowCheckpointWrite).where(WorkflowCheckpointWrite.thread_id == thread_id))
            db.execute(delete(WorkflowCheckpointBlob).where(WorkflowCheckpointBlob.thread_id == thread_id))
            db.execute(delete(WorkflowCheckpoint).where(WorkflowCheckpoint.thread_id == thread_id))
            db.commit()
        finally:
            db.close()

    # 版本号生成：与 InMemorySaver 保持一致
    def get_next_version(self, current: str | None, channel: None) -> str:
        """
        生成下一个通道版本号
            :param current: 当前版本号
            :param channel: 通道名（未使用）
            :return: 32 位递增序号 + 16 位随机小数的字符串
        """
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # 异步包装：LangGraph 异步调用时复用同步实现
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """异步读取检查点，直接复用同步实现"""
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ):
        """异步列出检查点，直接复用同步实现"""
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """异步写入检查点，直接复用同步实现"""
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """异步写入 pending writes，直接复用同步实现"""
        return self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """异步删除线程，直接复用同步实现"""
        return self.delete_thread(thread_id)

    # 内部：行转 CheckpointTuple
    def _to_tuple(
        self,
        db,
        row: WorkflowCheckpoint,
        config: RunnableConfig | None,
    ) -> CheckpointTuple:
        """
        把数据库行还原成 CheckpointTuple
            :param db: 数据库会话
            :param row: 检查点行
            :param config: 调用方原始配置；为空时构造新配置
            :return: CheckpointTuple
        """
        checkpoint: Checkpoint = self.serde.loads_typed(
            (row.checkpoint_type, row.checkpoint_blob)
        )
        # 1.按 channel_versions 回填通道值
        channel_values: dict[str, Any] = {}
        for channel, version in checkpoint.get("channel_versions", {}).items():
            blob = db.execute(
                select(WorkflowCheckpointBlob).where(
                    WorkflowCheckpointBlob.thread_id == row.thread_id,
                    WorkflowCheckpointBlob.checkpoint_ns == row.checkpoint_ns,
                    WorkflowCheckpointBlob.channel == channel,
                    WorkflowCheckpointBlob.version == str(version),
                )
            ).scalars().first()
            if blob is None or blob.type == "empty":
                continue
            channel_values[channel] = self.serde.loads_typed((blob.type, blob.blob))
        checkpoint["channel_values"] = channel_values

        # 2.读取 pending writes，按 LangGraph 约定排序
        write_rows = db.execute(
            select(WorkflowCheckpointWrite).where(
                WorkflowCheckpointWrite.thread_id == row.thread_id,
                WorkflowCheckpointWrite.checkpoint_ns == row.checkpoint_ns,
                WorkflowCheckpointWrite.checkpoint_id == row.checkpoint_id,
            )
        ).scalars().all()
        write_rows.sort(key=_writes_sort_key)
        pending_writes = [
            (item.task_id, item.channel, self.serde.loads_typed((item.type, item.blob)))
            for item in write_rows
        ]

        # 3.还原配置与父检查点
        normalized = {
            "configurable": {
                "thread_id": row.thread_id,
                "checkpoint_ns": row.checkpoint_ns,
                "checkpoint_id": row.checkpoint_id,
            }
        }
        parent_config = (
            {
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
            if row.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config=config or normalized,
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed((row.metadata_type, row.metadata_blob)),
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    # 内部：MySQL upsert
    @staticmethod
    def _upsert(db, table, values: dict[str, Any], update_columns: Sequence[str]) -> None:
        """
        MySQL INSERT ... ON DUPLICATE KEY UPDATE
            :param db: 数据库会话
            :param table: 目标表
            :param values: 插入值
            :param update_columns: 冲突时需要更新的列
        """
        stmt = mysql_insert(table).values(**values)
        stmt = stmt.on_duplicate_key_update(
            {column: stmt.inserted[column] for column in update_columns}
        )
        db.execute(stmt)



# 工厂：按配置返回检查点存储
def build_checkpointer():
    """
    构建检查点存储
        :return: MySQLCheckpointSaver 实例
    """
    return MySQLCheckpointSaver()
