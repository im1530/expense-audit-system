"""
审批数据模型（审批流定义 + 审批记录 + 审批状态枚举）
    支撑"逐级审批"的核心表结构，两张表分工明确：
        - ApprovalFlow：审批流模板，levels 用 JSON 描述每一级要求的角色，例如
          [{"level":1,"role":"manager"}, {"level":2,"role":"finance"}]，由提交接口负责匹配绑定；
        - Approval：审批轨迹，一条记录代表某人在某一级对某张报销单的一次动作，
          同时用 status 记录待审批、已撤回、已作废等流程痕迹，供待办列表与审批历史查询。
    状态区分：Expense.status 表示"单据整体走到哪一步"，Approval.status 表示"这一条审批记录的状态"，
    两者需要在同一个事务里同步更新，否则会出现"单据待审批但待办里查不到"的不一致。
    落库方式：Approval.status 直接以 ApprovalStatus 枚举声明，且 native_enum=True，
    与 schema.sql 的 MySQL ENUM 保持一致；新增取值必须同步写迁移（ALTER TABLE）。
"""



from app.models.base import Base
import enum
from sqlalchemy import Column, BigInteger, String, Text, Boolean, JSON, Enum, Integer, ForeignKey
from sqlalchemy.orm import relationship



# 审批流定义表
class ApprovalFlow(Base):
    """
    审批流定义表模型：一套审批流程模板，如「经理 → 财务」
        核心字段 levels（JSON 列表），每一项形如 {"level": 1, "role": "manager"}：
            - level：审批层级，从 1 开始连续递增，与 expenses.current_level 对应；
            - role：该层要求的审批人角色，取值为 manager / finance_other / finance / boss，
              其中 finance_other 表示"除申请人本人以外的任一财务"，由审批接口与待办接口共同解释。
        状态字段：
            - is_active：停用的流程不可再被匹配使用；
            - is_default：全表至多一条为 True，作为所有规则都不命中时的兜底流程。
        关联：expenses.flow_id 与 approvals.flow_id 都指向本表，删除前需先解除引用。
    """
    # 表名(显式指定)
    __tablename__ = 'approval_flows'        # 表名：审批流定义


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="审批流ID"       # 字段备注，写进数据库
    )


    # ===== 流信息 =====
    # name:审批流名称,如"普通报销审批流"
    name = Column(
        String(100),            # 字段类型：字符串，最长为100
        nullable=False,         # 非空:每条记录必填名称
        comment="审批流名称"      # 字段备注，写进数据库
    )

    # description:审批流说明
    description = Column(
        Text,                 # 字段类型：文本
        comment="审批流说明"    # 字段备注，写进数据库
    )

    # levels:各级审批配置,用 JSON 存一个列表,如 [{"level":1,"role":"manager"}, {"level":2,"role":"finance"}]
    levels = Column(
        JSON,                   # JSON 类型：直接存字典/列表，数据库用 json 列,SQLAlchemy 自动序列化
        comment="各级审批配置"    # 字段备注，写进数据库
    )


    # ===== 状态 =====
    # is_active:是否启用,停用的流程不可用
    is_active = Column(
        Boolean,                # 字段类型：布尔值
        default=True,           # 默认值：True，表示默认启用
        comment="是否启用"       # 字段备注，写进数据库
    )

    # is_default：标记是否为默认审批流
    is_default = Column(
        Boolean,                    # 字段类型：布尔值
        default=False,              # 默认值：False，表示默认不启用
        nullable=False,             # 非空:每条记录必填是否默认审批流
        comment="是否为默认审批流"     # 字段备注，写进数据库
    )


    # ===== 时间戳 =====
    # created_at 记录创建时间 / updated_at 记录最后修改时间 已经在 base.py里写好


    # ===== 调试打印显示直观信息 =====
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <ApprovalFlow(id=1,name=普通报销审批流)>
        """
        return f"<ApprovalFlow(id={self.id},name={self.name})>"



# 审批结果枚举
class ApprovalStatus(str,enum.Enum):
    """
    审批记录状态枚举（描述"一条审批记录"的处境，不是单据状态）
        取值语义：
            - pending    待审批：已生成待办，等待本级审批人处理
            - approved   已通过：本级审批人同意，多级流程中单据会继续流向下一级
            - rejected   已拒绝：本级审批人驳回，流程终止，单据置为已驳回
            - recalled   已撤回：提交人主动撤回提交，待办作废但记录留痕
            - cancelled  已撤销：提交人主动作废单据，未处理的待办同步作废
            - obsoleted  已作废：重新提交后，上一轮流程的旧记录统一失效标记
        与 Approval.status 列的关系：该列直接以本枚举做类型声明（native_enum=True，MySQL ENUM），
        新增取值时必须同步写迁移，避免代码能写、库拒绝。
    """
    PENDING = "pending"         # 待审批
    APPROVED = "approved"       # 已通过:审批通过
    REJECTED = "rejected"       # 已拒绝:审批被拒绝
    RECALLED = "recalled"       # 已撤回:用户撤回报销单的提交
    CANCELLED = "cancelled"     # 已撤销:用户主动撤销报销单
    OBSOLETED = "obsoleted"     # 已作废:用户重新重新提交报销单后，上一轮旧审批记录的失效标记



# 审批记录表
class Approval(Base):
    """
    审批记录表模型：某个人对某张报销单的一次审批动作，同时充当"待办条目"与"审批轨迹"
        两种用途复用同一条记录：
            - 待办：status=pending 的记录，配合单据当前层级与审批流配置，决定谁能看到、谁能审批；
            - 轨迹：记录全部状态（含撤回、撤销、作废），按 level + created_at 排序即可还原完整流转过程。
        关键字段：
            - approver_id 可空：提交时生成的待办还没有处理人，真正审批后才回填；
            - level：产生该记录的审批层级，与审批流 levels 中的 level 对应；
            - comment：审批意见或系统备注（如"单据提交，等待审批"）；
            - flow_id：所属审批流，保证轨迹能追溯到当时的流程模板。
    """
    # 表名(显式指定)
    __tablename__ = 'approvals'             # 表名：审批记录


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="审批记录ID"      # 字段备注，写进数据库
    )


    # ===== 外键:关联报销单 =====
    # expense_id:这条审批对应哪张报销单
    expense_id = Column(
        BigInteger,                 # 字段类型：大整数，适合做外键
        ForeignKey("expenses.id"),  # 外键：关联 expenses 表的 id 字段,必须是 expenses 表里已存在的id
        nullable=False,             # 非空:每条审批必须挂在某张报销单上
        index=True,                 # 建立索引,按 expense_id 查询更快
        comment="报销单ID"           # 字段备注，写进数据库
    )


    # ===== 外键:关联用户表 =====
    # approver_id:审批人是哪个用户 (待审批时还没人处理,所以可空)
    approver_id = Column(
        BigInteger,                 # 字段类型：大整数，适合做外键
        ForeignKey("users.id"),     # 外键：关联 users 表的 id 字段,必须是 users 表里已存在的id
        nullable=True,              # 可空
        index=True,                 # 建立索引,按 approver_id 查询更快
        comment="审批人ID，待审批/已撤回/已作废等未处理状态可为空，审批操作后回填"           # 字段备注，写进数据库
    )


    # ===== 外键:关联审批流 =====
    # flow_id:属于哪个审批流
    flow_id = Column(
        BigInteger,                       # 字段类型：大整数，适合做外键
        ForeignKey("approval_flows.id"),  # 外键：关联 approval_flows 表的 id 字段,必须是 approval_flows 表里已存在的id
        nullable=False,                   # 非空:每条审批必须属于某个审批流
        index=True,                       # 建立索引,按 flow_id 查询更快
        comment="审批流ID"                 # 字段备注，写进数据库
    )


    # ===== 审批状态 =====
    # status:本条审批记录的状态,取 ApprovalStatus 枚举值(待审批/通过/拒绝/撤回/撤销/作废)
    status = Column(
        Enum(
            ApprovalStatus,                 # 字段类型：枚举，只能取 ApprovalStatus 枚举里的值
            values_callable=lambda enum_cls: [m.value for m in enum_cls],   # 存枚举的 .value（小写），而非成员名大写
            native_enum=True,               # 与 schema.sql 的 MySQL ENUM 保持一致；新增状态必须同步写迁移
            name="approval_status",         # 数据库里枚举类型的名字
        ),
        nullable=False,                     # 非空:每条审批必须有审批状态
        index=True,                         # 建立索引,按 status 查询更快
        comment="审批记录状态"               # 字段备注，写进数据库
    )

    # level:当前是第几级审批(1、2、3...)
    level = Column(
        Integer,                # 字段类型：整数
        nullable=False,         # 非空:每条审批一定有审批层级
        comment="审批层级"       # 字段备注，写进数据库
    )

    # comment:审批意见,如如"同意""票据不全，退回"
    comment = Column(
        Text,                   # 字段类型：文本
        comment="审批意见"       # 字段备注，写进数据库
    )


    # ===== 时间戳 =====
    # created_at 记录创建时间 已经在 base.py里写好


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这条审批记录属于哪张报销单
    expense = relationship(
        "Expense",                  # 关联目标模型
        back_populates="approvals",           # 配对 Expense.approvals（报销单 → 其所有审批记录）
    )

    # 这条记录的审批人
    approver = relationship(
        "User",                     # 关联目标模型
        back_populates="approvals",           # 配对 User.approvals（用户 → 其所有审批记录）
    )


    # ===== 调试打印显示直观信息 =====
    def __repr__(self):
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <Approval(id=1, status=approved)>
        """
        return f"<Approval(id={self.id}, status={self.status})>"

