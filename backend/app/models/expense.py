"""
报销数据模型（报销单 + 报销明细 + 费用类别 + 两个枚举）
    报销业务的三张核心表，层级关系为：Expense(1) → ExpenseItem(N) → Category(1)：
        - Expense：报销单主单，承载单据号、标题、总金额、状态、当前审批层级、审批角色链、
          各关键节点时间，以及 AI 审核写入的风险分数/风险等级/审核结论；
        - ExpenseItem：报销明细（一条费用一行），记录说明、金额、费用发生日期与发票信息；
        - Category：费用类别（小类，如"高铁票"），归属 ExpenseType 大类，可配置单次报销限额。
    约定：
        - 金额一律用 Numeric(12, 2)，避免浮点误差；
        - total_amount 由后端按明细汇总维护（services.recalculate_expense_total_amount），
          不接受前端传入，明细增删改后必须重算，保证主表与明细表金额一致；
        - 单据状态用 ExpenseStatus 枚举，以字符串形式落库，便于查询与前端展示。
"""


import enum
from sqlalchemy import Column, BigInteger, String, Enum, ForeignKey, Numeric, Text, DateTime, Boolean, Date, Integer, \
    JSON
from app.models.base import Base
from sqlalchemy.orm import relationship



# 报销状态枚举：单据在审批流程中的各个阶段
class ExpenseStatus(str,enum.Enum):
    """
    报销状态枚举（描述"整张单据"走到哪一步）
    `str, enum.Enum`：让枚举值同时是字符串，存进 MySQL、接口返回都方便
    （否则默认是 `ExpenseStatus.DRAFT` 这种对象，序列化时还要额外转换）。
    状态机（仅允许以下流转，越级流转由接口层用 409 拦截）：
        draft → pending（提交送审）→ approved（终审通过）→ registered（财务登记）
        → paid（提交人确认领取，闭环完成）
        pending → draft（撤回提交）
        pending / rejected → cancelled（主动撤销）
        pending → rejected（任一级驳回）；rejected → pending（修改后重新提交）
        registered → approved（财务撤销登记）
    说明：每次流转都必须同步维护 submitted_at / approved_at / registered_at / paid_at / cancelled_at 中的对应时间字段，
    并保持与 approvals 表中审批记录状态一致。
    """
    DRAFT = "draft"                # 草稿：刚建好，还没提交
    PENDING = 'pending'            # 待审批：正在走审批
    APPROVED = "approved"          # 审批通过：审批同意
    REGISTERED = "registered"      # 已登记：财务登记，待提交人确认领取
    REJECTED = "rejected"          # 审批拒绝：审批打回
    PAID = "paid"                  # 已支付：提交人确认领取，报销闭环完成
    CLOSED = "closed"              # 已关闭：管理员强制关闭，未经过提交人确认
    CANCELLED = "cancelled"        # 已取消：主动作废



# 报销类型枚举:费用属于哪一类
class ExpenseType(str,enum.Enum):
    """
    费用性质大类枚举（Category.expense_type 的取值，也是列表筛选的一级条件）
        层级关系：ExpenseType（大类）→ Category（小类）→ ExpenseItem（具体一笔费用）。
        - 大类用于统计与一级筛选，小类用于表单选择与限额控制；
        - 取值以字符串落库，新增大类时必须同步更新 Expense/Category 相关字典接口的中文映射。
    """
    MEAL = "meal"                          # 餐饮费
    TRANSPORTATION = "transportation"      # 交通费
    ACCOMMODATION = "accommodation"        # 住宿费
    OFFICE = "office"                      # 办公费
    OTHER = "other"                        # 其他



# 报销单表
class Expense(Base):
    """
    报销单主表模型（一张报销单一行，是审批与支付的主体）
        字段分组：
            - 归属与编号：id、user_id（提交人）、expense_no（对外单号，唯一）、flow_id（绑定的审批流）
            - 金额与内容：total_amount（按明细汇总）、title、description、remark
            - 流程状态：status（见 ExpenseStatus）、current_level（当前审批层级）、
              approval_chain（匹配到的审批角色链，如 ["manager","finance"]）
            - 节点时间：submitted_at / approved_at / paid_at / cancelled_at，走到哪一步写哪个
            - AI 审核：risk_score（0~100）、risk_level（high/mid/low）、ai_reviewed_result（结论文本）
        行为：
            - is_editable：草稿才可编辑的便捷判断
            - items / approvals：明细与审批记录的级联关系（删单时一并删除）
        一致性要求：明细增删改后必须调用 recalculate_expense_total_amount 重算总金额；
        状态变更必须与 approvals 表记录同事务提交。
    """
    # 表名(显示写出,方便阅读)
    __tablename__ = "expenses"              # 表名：报销单


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="报销单ID"       # 字段备注，写进数据库
    )


    # ===== 外键: 关联用户表 =====
    user_id = Column(
        BigInteger,                 # 字段类型：大整数,和 users.id 类型一致
        ForeignKey("users.id"),     # 外键：关联 users 表的 id 字段,必须是 users 表里已存在的id
        nullable=False,             # 非空:每张单必填提交人
        index=True,                 # 建立索引,按 user_id 查询更快
        comment="提交人ID"           # 字段备注，写进数据库
    )

    # department_id:提交时的部门快照；人员调部门后，历史报表仍按提交时归属统计
    department_id = Column(
        BigInteger,                     # 与 departments.id 同类型
        ForeignKey("departments.id"),   # 外键：关联部门表
        nullable=True,                  # boss/admin 等可无部门
        index=True,                     # 按部门统计/筛选
        comment="提交时部门快照ID"       # 字段备注，写进数据库
    )


    # ===== 外键:关联审批流表 =====
    # flow_id:这张单走哪条审批流,外键→approval_flows.id,可空
    flow_id = Column(
        BigInteger,                         # 字段类型:大整数,和 approval_flows.id 一致
        ForeignKey("approval_flows.id"),    # 外键:关联 approval_flows 表的 id
        nullable=True,                      # 可空:暂未挂流时为空
        index=True,                         # 建立索引,按 flow_id 查询更快
        comment="审批流ID"                   # 字段备注,写进数据库
    )


    # ===== 当前审批层级 =====
    # current_level:当前批到第几级(初始1),多级流转时逐级+1
    current_level = Column(
        Integer,                # 字段类型:整数,存第几级
        nullable=False,         # 非空:每张单必须有当前层级
        default=1,              # 默认从第1级开始
        comment="当前审批层级"    # 字段备注,写进数据库
    )

    # approval_chain:匹配到的审批角色链(JSON列表),如["manager","finance"]
    approval_chain = Column(
        JSON,                     # 字段类型：JSON，存匹配到的审批角色链
        nullable=True,            # 可空:暂未匹配到审批角色链时为空
        comment="审批角色链"        # 字段备注，写进数据库
    )


    # ===== 报销基本信息 =====
    # expense_no：报销单号，全局唯一，用于对外编号
    expense_no = Column(
        String(50),             # 字段类型：字符串，长度为50
        nullable=False,         # 非空:每张报销单必填单号
        unique=True,            # 唯一约束:单号不能重复
        index=True,             # 建立索引,按 expense_no 查询更快
        comment="报销单号"       # 字段备注，写进数据库
    )

    # title: 报销标题,例如"10月份出差北京交通费"
    title = Column(
        String(200),            # 字段类型：字符串，长度为200
        nullable=False,         # 非空:每张报销单必填标题
        comment="报销标题"       # 字段备注，写进数据库
    )


    # ===== 金额信息 =====
    # total_amount：总金额，用 Numeric 金额专用定点数，避免浮点误差
    total_amount = Column(
        Numeric(12, 2),         # 字段类型：Numeric，总金额，12位数字，其中2位小数
        nullable=False,                                 # 非空:每张报销单必填总金额
        comment="总金额"                                 # 字段备注，写进数据库
    )


    # ===== 单据状态 =====
    # status:报销状态,默认新建的报销单就是草稿 DRAFT
    status = Column(
        Enum(
            ExpenseStatus,              # 字段类型：枚举，只能取 ExpenseStatus 枚举里的值
            values_callable=lambda enum_cls: [m.value for m in enum_cls], # 存枚举的 .value（小写），而非成员名大写
            native_enum=True,           # 与 schema.sql 的 MySQL ENUM 保持一致；新增状态必须同步写迁移
            name="expense_status",      # 数据库里枚举类型的名字
        ),
        default=ExpenseStatus.DRAFT,    # 默认值：草稿
        nullable=False,                 # 非空:每张报销单必填报销状态
        index=True,                     # 建立索引,按 status 查询更快
        comment="报销状态"               # 字段备注，写进数据库
    )


    # ===== 描述信息 =====
    # description:报销事由/说明,内容可能较长,用 Text
    description = Column(
        Text,                         # 字段类型：Text，不限长度(区别于 String 要设长度上线)
        comment="报销说明"             # 字段备注，写进数据库
    )

    # remark:附加备注
    remark = Column(
        Text,                         # 字段类型：Text
        comment="备注"                 # 字段备注，写进数据库
    )


    # ===== 关键时间点信息（都可空，到对应环节才填） =====
    # submitted_at:提交时间,点"提交"时写入
    submitted_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="提交时间"                   # 字段备注，写进数据库
    )

    # approved_at:审批通过时间
    approved_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="审批时间"                   # 字段备注，写进数据库
    )

    # registered_at:财务登记时间（登记后待提交人确认领取）
    registered_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="财务登记时间"               # 字段备注，写进数据库
    )

    # registered_by:登记财务的用户ID
    registered_by = Column(
        BigInteger,                         # 与 users.id 同类型
        comment="登记财务ID"                 # 字段备注，写进数据库
    )

    # register_note:登记备注（会发给提交人）
    register_note = Column(
        String(500),                        # 与迁移保持一致
        comment="登记备注"                   # 字段备注，写进数据库
    )

    # paid_at:报销闭环完成时间（提交人确认领取时写入）
    paid_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="支付时间"                   # 字段备注，写进数据库
    )

    # confirmed_at:提交人确认领取时间
    confirmed_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="确认领取时间"               # 字段备注，写进数据库
    )

    # closed_at:管理员强制关闭时间（closed 终态）
    closed_at = Column(
        DateTime(timezone=True),            # 日期时间类型,timezone=True 带时区
        comment="强制关闭时间"               # 字段备注，写进数据库
    )

    # cancelled_at:单据撤销时间
    cancelled_at = Column(
        DateTime(timezone=True),        # 日期时间类型,带时区
        comment="撤销时间",              # 字段备注，写进数据库
    )

    # created_at 记录创建时间 / updated_at 记录最后修改时间 已经在 base.py里写好


    # ===== AI 审核信息（风险评估 Agent 写入）=====
    # risk_score:风险分数,0~100之间,保留2位 --- 算出 risk_score 后归档给 risk_level 确定等级
    risk_score = Column(
        Numeric(5, 2),  # 字段类型：Numeric，风险分数，3位数字，其中2位小数
        comment="风险分数"  # 字段备注，写进数据库
    )

    # risk_level:风险等级,如high/mid/low
    risk_level = Column(
        String(20),             # 字段类型：字符串，长度为20
        comment="风险等级"       # 字段备注，写进数据库
    )

    # risk_source:风险结论来源：ai（模型判断）/ rules（规则兜底）
    risk_source = Column(
        String(20),             # 字段类型：字符串，长度为20
        comment="风险结论来源"    # 字段备注，写进数据库
    )

    # ai_reviewed_result: AI 给出的审核结论文本
    ai_reviewed_result = Column(
        Text,                          # 字段类型：Text
        comment="AI审核结果"         # 字段备注，写进数据库
    )


    # ===== 便捷判断属性 =====
    # 报销单是否可编辑
    @property
    def is_editable(self):
        """
        是否可编辑（便捷属性）：仅草稿状态可编辑
            注意：业务接口实际放行的是「草稿 + 已驳回」两种状态（驳回后可改后重提），
            因此判断"能否改单"时不要只看本属性，应比照接口中的状态校验。
            :return: status 为 draft 时返回 True
        """
        return self.status == ExpenseStatus.DRAFT


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这张报销单属于哪个提交人（多对一：多张单对应一个用户）
    user = relationship(
        "User",               # 关联目标模型
        back_populates="expenses",     # 配对 User.expenses（人 → 其所有的报销单）
    )

    # 这张单包含的多条费用明细；删单时级联删除明细
    items = relationship(
        "ExpenseItem",          # 关联目标模型
        back_populates="expense",         # 配对 ExpenseItem.expense（报销单 → 其所有明细）
        cascade="all, delete-orphan"      # 级联操作：所有操作都级联，包括删除
    )

    # 这张单的审批记录
    approvals = relationship(
        "Approval",             # 关联目标模型
        back_populates="expense",         # 配对 Approval.expense（审批记录 → 其所属报销单）
        cascade="all, delete-orphan"      # 级联操作：所有操作都级联，包括删除
    )

    # 这张单的规则命中结果（每次提交/重新提交先清空再写入）
    rule_hits = relationship(
        "ExpenseRuleHit",       # 关联目标模型
        back_populates="expense",       # 配对 ExpenseRuleHit.expense
        cascade="all, delete-orphan",   # 删单时一并清理命中记录
        order_by="ExpenseRuleHit.id",   # 按写入顺序输出，前端展示稳定
    )


    # ===== 调试打印显示直观信息 =====
    # 调试打印用：__repr__ 是 Python 的 "对象自我描述" 方法，决定这个对象被 print、在调试器里查看、放进列表打印时显示成什么字符串
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看或放进列表打印时的显示内容
            :return: 形如 <Expense(id=1,expense_no=EXP2024...,status=ExpenseStatus.DRAFT)>
        """
        return f"<Expense(id={self.id},expense_no={self.expense_no},status={self.status})>"



# 报销项目表
class ExpenseItem(Base):
    """
    报销明细表模型（报销单下的一条费用，一张单至少一条）
        职责：承载真正"花了多少钱、花在哪、什么时候花的"的颗粒度数据，
        主单总金额、按费用大类/小类的筛选与统计，全部依赖本表。
        字段分组：
            - 归属：expense_id（所属报销单）、category_id（费用小类，指向 categories）
            - 内容：description（费用说明）、amount（金额）、expense_date（费用发生日期）
            - 票据：invoice_no（发票号）；凭证文件走 attachments 关联表，不再在明细上存 URL
        约束：amount / expense_date / description / expense_id 必填；日期不得晚于今天由 schema 层校验。
        一致性要求：本表任何增删改之后，都要重算所属报销单的 total_amount。
    """
    # 表名(显示写出,方便阅读)
    __tablename__ = "expense_items"         # 表名：报销明细


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="报销项目ID"     # 字段备注，写进数据库
    )


    # ===== 外键:关联报销单 =====
    # expense_id 指向 expenses.id，表示这条明细属于哪张单
    expense_id = Column(
        BigInteger,                     # 字段类型：大整数,和 expenses.id 类型一致
        ForeignKey("expenses.id"),      # 外键：关联 expenses 表的 id 字段,必须是 expenses 表里已存在的id
        nullable=False,                 # 非空:每条明细必填所属单
        index=True,                     # 建立索引,按 expense_id 查询更快
        comment="所属报销单ID"            # 字段备注，写进数据库
    )


    # ===== 外键:关联费用类别 =====
    # category_id 指向 categories.id，表示这条明细属于哪类费用
    category_id = Column(
        BigInteger,                       # 字段类型：大整数,和 categories.id 类型一致
        ForeignKey("categories.id"),      # 外键：关联 categories 表的 id 字段,必须是 categories 表里已存在的id
        index=True,                       # 建立索引,按 category_id 查询更快
        comment="费用类别ID"               # 字段备注，写进数据库
    )


    # ===== 项目信息 =====
    # description：这条费用的说明，如"北京-高铁票"
    description = Column(
        String(500),            # 字段类型：字符串，长度为500
        nullable=False,         # 非空:每条明细必填说明
        comment="费用说明"       # 字段备注，写进数据库
    )

    # amount:这条明细的金额，如"1000.00"
    amount = Column(
        Numeric(12, 2),         # 字段类型：Numeric，金额，12位数字，其中2位小数
        nullable=False,                                 # 非空:每条明细必填金额
        comment="金额"                                   # 字段备注，写进数据库
    )

    # expense_date:费用发生日期(只有年月日)
    expense_date = Column(
        Date,                           # 字段类型：日期,YYYY-MM-DD
        nullable=False,                 # 非空:每条明细必填费用发生日期
        comment="费用发生日期"            # 字段备注，写进数据库
    )


    # ===== 发票信息 =====
    # invoice_no:发票编号，可空
    invoice_no = Column(
        String(100),            # 字段类型：字符串，长度为100
        comment="发票编号"       # 字段备注，写进数据库
    )

    # no_invoice_reason:无票说明(没有发票时填写原因,规则引擎据此判断是否提示)
    no_invoice_reason = Column(
        String(200),                    # 字段类型：字符串，长度200
        comment="无票说明(无发票时填写原因)"  # 字段备注，写进数据库
    )


    # ===== 时间戳 =====
    # created_at 记录创建时间 已经在 base.py里写好


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这条明细属于哪张报销单
    expense = relationship(
        "Expense",              # 关联目标模型
        back_populates="items",           # 配对 Expense.items（报销单 → 其所有明细）
    )

    # 这条明细属于哪类费用
    category = relationship(
        "Category",             # 关联目标模型
        back_populates="items",           # 配对 Category.items（费用类别 → 其所有明细）
    )

    # 这条明细下的发票/附件（一张明细可以有多张票）
    # 删除明细时级联删除附件元数据（DB 外键也配了 ON DELETE CASCADE，双保险）
    attachments = relationship(
        "Attachment",                   # 关联目标模型
        back_populates="item",          # 配对 Attachment.item（附件 → 所属明细）
        cascade="all, delete-orphan",   # 级联删除孤儿附件记录
    )

    # ===== 调试打印显示直观信息 =====
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <ExpenseItem(id=1,amount=120.00)>
        """
        return f"<ExpenseItem(id={self.id},amount={self.amount})>"



# 费用类别表
class Category(Base):
    """
    费用类别表模型（费用小类，是 ExpenseType 大类下的可选项）
        用途：
            - 报销明细表单里"费用类别"下拉框的数据源（仅返回 is_active = True 的项）；
            - 列表接口按费用大类/小类筛选的关联依据；
            - max_amount 为该类别单次报销限额，为空表示不限额（限额校验可在规则引擎中实现）。
        字段：
            - name 中文名称、code 唯一编码（程序识别用，如 travel_bus）、expense_type 所属大类；
            - description 说明、is_active 启停标记（停用后不可再被选择，历史数据仍保留）。
    """
    # 表名(显示写出,方便阅读)
    __tablename__ = "categories"            # 表名：费用类别


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="费用类别ID"         # 字段备注，写进数据库
    )


    # ===== 类别信息 =====
    # name:类别名称,如"差旅费"
    name = Column(
        String(100),            # 字段类型：字符串，长度为100
        nullable=False,         # 非空:每类别必填名称
        comment="类别名称"       # 字段备注，写进数据库
    )

    # code:类别代码,如"travel_bus",唯一,便于程序识别
    code = Column(
        String(50),             # 字段类型：字符串，长度为50
        nullable=False,         # 非空:每类别必填代码
        unique=True,            # 唯一约束:代码不能重复
        comment="类别代码"       # 字段备注，写进数据库
    )

    # expense_type:费用性质大类，关联 ExpenseType 枚举
    expense_type = Column(
        Enum(
            ExpenseType,                 # 字段类型：枚举，只能取 ExpenseType 枚举里的值
            values_callable=lambda enum_cls: [m.value for m in enum_cls],   # 存 .value（小写），而非成员名大写
            native_enum=False,           # 关键：禁用数据库原生ENUM，底层用varchar存储
            name="expense_type",         # 数据库里枚举类型的名字
        ),
        nullable=False,                   # 非空：每个细分类必须归属一个大类
        comment="所属费用性质大类"          # 字段备注
    )

    # max_amount:该类别单次报销最高限额,可以为空表示不限额
    max_amount = Column(
        Numeric(12, 2),         # 字段类型：Numeric，金额，12位数字，其中2位小数
        comment="单次最高限额"                             # 字段备注，写进数据库
    )

    # description:类别说明,内容可能较长,用 Text
    description = Column(
        Text,                         # 字段类型：Text，不限长度(区别于 String 要设长度上线)
        comment="类别说明"             # 字段备注，写进数据库
    )


    # ===== 状态 =====
    is_active = Column(
        Boolean,                    # 字段类型：布尔值
        default=True,               # 默认启用
        comment="是否启用"           # 字段备注，写进数据库
    )


    # ===== 时间戳 =====
    # created_at 记录创建时间 已经在 base.py里写好


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这个类别下的所有费用明细
    items = relationship(
        "ExpenseItem",          # 关联目标模型
        back_populates="category",        # 配对 ExpenseItem.category（费用明细 → 其所属类别）
    )

    # ===== 调试打印显示直观信息 =====
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <Category(id=1,name=高铁票)>
        """
        return f"<Category(id={self.id},name={self.name})>"
