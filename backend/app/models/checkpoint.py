"""
LangGraph 检查点持久化模型（Phase F：MySQL Checkpointer）
    为什么单独建三张表：
        - workflow_checkpoints：保存一次检查点的元数据与除 channel_values 外的状态；
        - workflow_checkpoint_blobs：按 (线程, 命名空间, channel, 版本) 保存通道值；
        - workflow_checkpoint_writes：保存节点写入（pending writes），用于故障恢复。
    与 MemorySaver 的对应关系：
        - 三张表分别对应 InMemorySaver 的 storage / blobs / writes 三个字典；
        - 复合主键保证同一线程同一检查点幂等写入；
        - 大字段用 LONGBLOB，避免 msgpack 序列化结果被截断。
    注意：
        - 本表只存 LangGraph 运行时状态，审计摘要仍以 workflow_runs / workflow_steps 为准；
        - checkpoint 可能包含脱敏后的单据上下文，访问权限应与 ai_review_logs 保持一致。
"""
from sqlalchemy import Column, Integer, LargeBinary, String

from app.models.base import Base



# 检查点主表
class WorkflowCheckpoint(Base):
    """
    LangGraph 检查点（一个线程 + 命名空间 + 检查点ID 唯一）
        :ivar thread_id: LangGraph 线程ID（当前等于 workflow_runs.id）
        :ivar checkpoint_ns: 命名空间，单图默认空串
        :ivar checkpoint_id: 检查点ID（LangGraph 生成，单调递增）
        :ivar parent_checkpoint_id: 父检查点ID，用于回溯链
        :ivar checkpoint_type: 序列化类型（msgpack/json）
        :ivar checkpoint_blob: 序列化后的检查点（不含 channel_values）
        :ivar metadata_type: 元数据序列化类型
        :ivar metadata_blob: 序列化后的元数据
    """
    __tablename__ = "workflow_checkpoints"                  # 表名：检查点

    thread_id = Column(String(128), primary_key=True, comment="LangGraph 线程ID")
    checkpoint_ns = Column(String(128), primary_key=True, default="", comment="检查点命名空间")
    checkpoint_id = Column(String(64), primary_key=True, comment="检查点ID")
    parent_checkpoint_id = Column(String(64), nullable=True, comment="父检查点ID")
    checkpoint_type = Column(String(32), nullable=False, comment="检查点序列化类型")
    checkpoint_blob = Column(LargeBinary, nullable=False, comment="检查点序列化数据")
    metadata_type = Column(String(32), nullable=False, comment="元数据序列化类型")
    metadata_blob = Column(LargeBinary, nullable=False, comment="元数据序列化数据")



# 通道值表
class WorkflowCheckpointBlob(Base):
    """
    检查点里的单个通道值
        :ivar thread_id: 线程ID
        :ivar checkpoint_ns: 命名空间
        :ivar channel: 通道名
        :ivar version: 通道版本号
        :ivar type: 序列化类型；empty 表示该通道本轮没有值
        :ivar blob: 序列化后的通道值
    """
    __tablename__ = "workflow_checkpoint_blobs"             # 表名：通道值

    thread_id = Column(String(128), primary_key=True, comment="LangGraph 线程ID")
    checkpoint_ns = Column(String(128), primary_key=True, default="", comment="检查点命名空间")
    channel = Column(String(128), primary_key=True, comment="通道名")
    version = Column(String(64), primary_key=True, comment="通道版本号")
    type = Column(String(32), nullable=False, comment="序列化类型")
    blob = Column(LargeBinary, nullable=False, comment="序列化后的通道值")



# 节点写入表
class WorkflowCheckpointWrite(Base):
    """
    节点执行产生的 pending write
        :ivar thread_id: 线程ID
        :ivar checkpoint_ns: 命名空间
        :ivar checkpoint_id: 检查点ID
        :ivar task_id: 任务ID
        :ivar idx: 同一任务内的写入序号；特殊通道用负数
        :ivar channel: 目标通道
        :ivar type: 序列化类型
        :ivar blob: 序列化后的写入值
        :ivar task_path: 任务路径，用于恢复时排序
    """
    __tablename__ = "workflow_checkpoint_writes"            # 表名：节点写入

    thread_id = Column(String(128), primary_key=True, comment="LangGraph 线程ID")
    checkpoint_ns = Column(String(128), primary_key=True, default="", comment="检查点命名空间")
    checkpoint_id = Column(String(64), primary_key=True, comment="检查点ID")
    task_id = Column(String(128), primary_key=True, comment="任务ID")
    idx = Column(Integer, primary_key=True, comment="写入序号")
    channel = Column(String(128), nullable=False, comment="目标通道")
    type = Column(String(32), nullable=False, comment="序列化类型")
    blob = Column(LargeBinary, nullable=False, comment="序列化后的写入值")
    task_path = Column(String(512), nullable=False, default="", comment="任务路径")
