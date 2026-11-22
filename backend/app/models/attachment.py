"""
附件数据模型（attachments 表）
    存放报销明细下的发票/凭证文件元数据，一张明细可以有多张票（原 1:1 invoice_url 已删除）：
        - 文件本身放在磁盘/对象存储，库里只存相对路径（file_path），换域名或迁 OSS 时不用改数据；
        - content_type / file_size 在上传时校验并落库，避免下载时再猜类型；
        - is_deleted 软删：替换发票或用户删除时只置标记，物理文件由后续清理任务回收，
          这样"DB 写失败但文件已删"的情况不会发生。
    关系：Attachment N:1 ExpenseItem（明细删除时数据库层 ON DELETE CASCADE 一并清理元数据）。
"""
from sqlalchemy import Column, BigInteger, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from app.models.base import Base



# 附件表
class Attachment(Base):
    """
    发票/附件表模型：一条记录对应一个已上传的文件
        字段说明：
            - expense_item_id：所属报销明细，明细删除时附件元数据级联删除（DB 外键 ON DELETE CASCADE）
            - file_name：用户上传时的原始文件名（仅用于展示，不参与磁盘路径）
            - file_path：相对存储路径，如 invoices/2026/11/<uuid>.pdf
            - content_type / file_size：上传时校验过的 MIME 与字节数
            - uploaded_by：上传人，用于鉴权与审计
            - is_deleted：软删标记，true 表示已作废，不再出现在列表与下载接口里
    """
    # 表名(显式写出)
    __tablename__ = "attachments"           # 表名：附件

    # ===== 主键 =====
    id = Column(
        BigInteger,                         # 大整数
        primary_key=True,                   # 主键
        index=True,                         # 建索引
        autoincrement=True,                 # 自增
        comment="附件ID"                     # 字段备注
    )

    # ===== 外键:关联报销明细 =====
    expense_item_id = Column(
        BigInteger,                                     # 与 expense_items.id 同类型
        ForeignKey("expense_items.id", ondelete="CASCADE"),  # 明细删除时级联删除附件元数据
        nullable=False,                                 # 非空：附件必须挂在某条明细上
        index=True,                                     # 建索引：按明细查附件
        comment="所属报销明细ID"                          # 字段备注
    )

    # ===== 文件元数据 =====
    file_name = Column(
        String(255),                        # 原始文件名(展示用)
        nullable=False,                     # 非空
        comment="原始文件名"                  # 字段备注
    )

    file_path = Column(
        String(500),                        # 相对存储路径
        nullable=False,                     # 非空
        comment="相对存储路径"                # 字段备注
    )

    content_type = Column(
        String(100),                        # MIME 类型
        nullable=False,                     # 非空
        comment="MIME类型"                   # 字段备注
    )

    file_size = Column(
        BigInteger,                         # 字节数
        nullable=False,                     # 非空
        comment="文件字节数"                  # 字段备注
    )

    # ===== 上传人与状态 =====
    uploaded_by = Column(
        BigInteger,                         # 上传人用户ID
        ForeignKey("users.id"),             # 关联用户表
        nullable=False,                     # 非空
        index=True,                         # 建索引：按上传人查询
        comment="上传人ID"                   # 字段备注
    )

    is_deleted = Column(
        Boolean,                            # 布尔
        nullable=False,                     # 非空
        default=False,                      # 默认未删除
        comment="是否已删除(软删)"            # 字段备注
    )

    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这条附件属于哪条报销明细
    item = relationship(
        "ExpenseItem",                      # 关联目标模型
        back_populates="attachments",       # 配对 ExpenseItem.attachments
    )

    # ===== 调试打印显示直观信息 =====
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <Attachment(id=1,file=invoice.pdf,size=12345)>
        """
        return f"<Attachment(id={self.id},file={self.file_name},size={self.file_size})>"
