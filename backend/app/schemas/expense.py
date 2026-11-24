"""
报销模块数据模型（费用明细、报销单、审批待办、通用字典、费用分类）
    覆盖报销全流程的入参校验与出参序列化，按业务实体分为五组：
        - 费用明细组：ExpenseItemBase / Create / Update / Response / ListItemResponse / ListResponse，
          内置"费用发生日期不得晚于今天"校验与"金额必须为正数"约束；
        - 报销单主单组：ExpenseBase / Create / Update / Response / ListResponse，
          创建时必须至少携带一条明细；单号、总金额、状态、审批链等均由后端生成，不接受前端传入；
        - 审批待办组：PendingApprovalItem / PendingApprovalListResponse，专为审批工作台设计的精简出参；
        - 通用字典：DictOption，费用大类、报销状态、审批结果、角色等所有枚举下拉的统一结构；
        - 费用分类：CategoryResponse，费用小类下拉与"大类 → 小类"级联选择的数据结构。
    所有响应模型均启用 from_attributes，可直接把 SQLAlchemy 实体交给 response_model 序列化；
    字段暴露范围即接口契约，新增敏感字段时务必确认是否需要同时扩展出参模型。
"""



from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from app.models.expense import ExpenseStatus, ExpenseType,Category
from app.schemas.rule import RuleHitItem, RuleRiskMark



# ============ 报销项目（明细）模式 ============
# 报销项目明细公共字段
class ExpenseItemBase(BaseModel):
    """
    费用明细公共字段（创建入参与明细出参共用）
        :ivar category_id: 费用类别ID，必须指向已启用的 categories 记录（接口层校验）
        :ivar description: 费用说明，1~500 字，如"北京-高铁票"
        :ivar amount: 明细金额，Decimal 类型且必须大于 0，避免浮点误差
        :ivar expense_date: 费用发生日期，不得晚于今天（见 validate_expense_date）
        :ivar invoice_no: 发票编号，选填，最长 100
        :ivar no_invoice_reason: 无票说明，选填，最长 200；填写后"发票缺失"规则不再提示
    """
    category_id: int = Field(..., description="费用类别ID")                     # 必填，类别必须存在且启用
    description: str = Field(..., min_length=1, max_length=500, description="费用说明")  # 必填，1~500 字
    amount: Decimal = Field(..., gt=0, description="费用金额")      # Decimal（金额类型）;  gt=0 → 必须正数
    expense_date: date = Field(..., description="费用发生日期")                 # 必填，不得晚于今天
    invoice_no:Optional[str] = Field(None, max_length=100, description="发票编号")  # 选填，最长 100
    no_invoice_reason: Optional[str] = Field(None, max_length=200, description="无票说明(没有发票时填写原因)")  # 选填，填了就不再提示缺票

    # 校验费用发生日期不能晚于今天
    @field_validator("expense_date")  # 声明：下面这个函数用来校验 expense_date 字段
    @classmethod                      # 类方法，Pydantic v2 要求这样写，不用创建实例
    def validate_expense_date(cls, v: date) -> date:
        """
        校验费用发生日期不得晚于今天（禁止报销"未来"的费用）
            校验失败时抛 ValueError，Pydantic 会把它包装成 422 校验错误并指明字段位置。
            :param v: 前端传入的 expense_date 值（已由 Pydantic 转成 date 类型）
            :return: 校验通过时原样返回，交由 Pydantic 继续组装模型
            :raises ValueError: 日期晚于今天
        """
        # cls：当前这个模式类；v：前端传进来的 expense_date 值
        # 1.晚于今天直接报错（禁止报销"未来"的费用）
        if v > date.today():  # 如果传入日期 晚于 今天
            raise ValueError("费用发生日期不能晚于今天")  # 抛错，请求被拦截
        # 2.校验通过，把原值交还给 Pydantic
        return v  # 校验通过，把原值交还给 Pydantic


# 创建报销项目明细
class ExpenseItemCreate(ExpenseItemBase):
    """
    创建明细入参模型（新建报销单时随主单一起提交，或单独追加一条明细时使用）
        字段与校验规则全部来自 ExpenseItemBase，本类只作为"语义占位"存在：
        将来若创建场景需要额外字段（如临时附件token），在这里追加而不影响出参模型。
    """
    pass


# 更新报销项目明细
class ExpenseItemUpdate(BaseModel):
    """
    更新明细入参模型（增量更新：传哪个字段改哪个，未传的保持原值）
        全部字段可选，与主单更新模型配合使用；接口侧用 exclude_unset=True 只取用户真正提交的字段。
        注意：这里不继承 ExpenseItemBase，因为基类字段都是必填，而增量更新必须允许缺省。
        :ivar category_id: 费用类别ID，修改后会重新校验该分类是否存在且启用
        :ivar description: 费用说明
        :ivar amount: 明细金额，必须大于 0
        :ivar expense_date: 费用发生日期
        :ivar invoice_no: 发票编号
        :ivar no_invoice_reason: 无票说明，填写后"发票缺失"规则不再提示
    """
    category_id: Optional[int] = None                  # 费用类型ID
    description: Optional[str] = Field(None, min_length=1, max_length=500)  # 描述说明
    amount: Optional[Decimal] = Field(None, gt=0)      # 费用金额
    expense_date: Optional[date] = None                # 费用发生日期
    invoice_no: Optional[str] = Field(None, max_length=100)  # 发票编号
    no_invoice_reason: Optional[str] = Field(None, max_length=200)  # 无票说明


# 返回给前端的报销项目明细
class ExpenseItemResponse(ExpenseItemBase):
    """
    明细出参模型（报销单详情里的 items 数组、明细接口的返回项）
        在基础字段之上追加服务端生成的只读字段，前端提交时无需也不能传这些值。
        :ivar id: 明细ID
        :ivar expense_id: 所属报销单ID
        :ivar created_at: 创建时间
    """
    id: int                     # 明细ID
    expense_id: int             # 所属报销单ID
    created_at: datetime        # 创建时间

    #允许 Pydantic 直接从 SQLAlchemy ORM 对象读属性转成响应模型。没有它，传一个数据库对象进来会报错，得手动一个个取字段
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性


# 报销明细列表项响应模型
class ExpenseItemListItemResponse(ExpenseItemResponse):
    """
    明细列表项出参模型（跨单据的明细列表接口使用，如「我的明细」「本部门明细」）
        继承明细全部字段，并追加所属单据与分类的上下文信息，避免前端为每条明细再发一次请求。
        :ivar expense_no: 所属报销单号，用于跳转/展示归属
        :ivar category_name: 费用分类（小类）中文名称
        :ivar expense_type: 所属费用性质大类，字符串形式（如 "meal"）
    """
    expense_no: str  # 所属报销单号，上下文展示用
    category_name: str  # 费用分类名称，前端直接展示中文
    expense_type: str  # 所属费用性质大类

    #允许 Pydantic 直接从 SQLAlchemy ORM 对象读属性转成响应模型。没有它，传一个数据库对象进来会报错，得手动一个个取字段
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性


# 报销明细分页响应模型
class ExpenseItemListResponse(BaseModel):
    """
    明细分页响应模型（明细列表接口的统一返回外壳）
        与报销单分页结构保持同构，前端可用同一套分页组件渲染两类列表。
        :ivar items: 当前页的明细列表
        :ivar total: 符合条件的总条数（不是本页条数）
        :ivar page: 当前页码，从 1 开始
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数，由 utils.pagination.calculate_total_pages 计算
    """
    items: List[ExpenseItemListItemResponse]    # 当前页的报销明细列表
    total: int                              # 符合条件的总条数
    page: int                               # 当前页码(从1开始)
    page_size: int                          # 每页条数
    total_pages: int                        # 总页数



# ============ 报销单模式 ============
# 报销单公共字段
class ExpenseBase(BaseModel):
    """
    报销单公共字段（创建入参与出参共用）
        :ivar title: 报销标题，1~200 字，如"10月份出差北京交通费"
        :ivar description: 报销说明/事由，选填，内容可能较长
        :ivar remark: 附加备注，选填
    """
    title: str = Field(..., min_length=1, max_length=200, description="报销标题")   # 必填，1~200 字
    description:Optional[str] = Field(None, description="报销说明")                # 选填，报销说明
    remark:Optional[str] = Field(None, description="附加备注")                    # 选填，附加备注


# 创建报销单
class ExpenseCreate(ExpenseBase):
    """
    创建报销单入参模型（只接收用户能决定的内容，其余一律由后端生成）
        后端自动生成/计算：expense_no、total_amount、status、user_id、flow_id、current_level、各时间字段。
        :ivar items: 报销项目明细列表，至少一条（使用 min_length 而非已废弃的 min_items）
    """
    items: List[ExpenseItemCreate] = Field(..., min_length=1, description="报销项目列表,至少一条")  # 必填，至少一条明细


# 更新报销单
class ExpenseUpdate(BaseModel):
    """
    更新报销单入参模型（增量更新：只改前端实际传入的字段）
        仅草稿与已驳回状态的单据允许调用；接口用 exclude_unset=True 提取字段，再逐个 setattr 回写。
        注意：本模型虽含 items 字段，但主单更新接口会显式丢弃它——明细的增删改走独立接口，
        避免"整单覆盖"与"逐条维护"两套逻辑互相冲突。
        :ivar title: 报销标题
        :ivar description: 报销说明
        :ivar remark: 附加备注
        :ivar items: 明细更新列表（主单更新接口不使用）
    """
    title: Optional[str] = Field(None, min_length=1, max_length=200)  # 报销标题（不传表示不修改）
    description: Optional[str] = None       # 报销说明（不传表示不修改）
    remark: Optional[str] = None            # 附加备注（不传表示不修改）
    items: Optional[List[ExpenseItemUpdate]] = None  # 明细列表（不传表示不修改明细）


# 返回给前端的报销单
class ExpenseResponse(ExpenseBase):
    """
    报销单完整出参模型（详情、创建、修改、状态流转等接口的统一返回结构）
        包含主单全部字段 + 明细列表，前端拿到即可直接渲染详情页。
        :ivar id: 报销单ID
        :ivar user_id: 提交人ID（后端从登录态写入，不接收前端传值）
        :ivar expense_no: 报销单号（后端按 EXP + 时间戳 + 随机码生成）
        :ivar total_amount: 报销总金额（后端按明细汇总，明细变动后自动重算）
        :ivar status: 报销状态枚举，由后端状态机流转
        :ivar risk_level: AI 风险等级（high/mid/low），由风险评估 Agent 写入
        :ivar risk_score: AI 风险分数，0~100
        :ivar ai_review_result: AI 审核结论文本（对应 ORM 字段 ai_reviewed_result，两者命名需统一）
        :ivar submitted_at: 提交时间，撤回后置空
        :ivar approved_at: 终审通过时间（任一级驳回时也会记录，用于审计）
        :ivar paid_at: 财务支付时间
        :ivar cancelled_at: 主动撤销时间
        :ivar created_at: 创建时间
        :ivar updated_at: 最后更新时间
        :ivar items: 关联的明细列表，随单据一起返回
        :ivar flow_id: 绑定的审批流ID；boss 提交的单据不走线上流程时为 null
        :ivar current_level: 当前审批层级；无线上流程时置 0
        :ivar approval_chain: 匹配到的审批角色链，如 ["manager","finance"]
    """
    id: int                                     # 报销单ID
    user_id: int                                # 提交人ID(后端填入)
    department_id: Optional[int] = None         # 提交时部门快照
    expense_no: str                             # 报销单号(后端生成)
    total_amount: Decimal                       # 报销总金额(后端汇总,前端不传)
    status: ExpenseStatus                       # 报销状态(后端流转,前端不传)
    risk_level: Optional[str] = None            # AI风险等级
    risk_source: Optional[str] = None           # 风险来源：ai/rules
    risk_score: Optional[Decimal] = None        # AI风险分数
    ai_reviewed_result: Optional[str] = None    # AI审核结论
    submitted_at: Optional[datetime] = None     # 提交时间
    approved_at: Optional[datetime] = None      # 审批时间
    registered_at: Optional[datetime] = None    # 财务登记时间
    registered_by: Optional[int] = None         # 登记财务ID
    register_note: Optional[str] = None         # 登记备注
    paid_at: Optional[datetime] = None          # 财务支付时间
    confirmed_at: Optional[datetime] = None     # 提交人确认领取时间
    closed_at: Optional[datetime] = None        # 管理员强制关闭时间
    cancelled_at: Optional[datetime] = None     # 作废时间
    created_at: datetime                        # 创建时间
    updated_at: Optional[datetime] = None       # 更新时间
    items: List[ExpenseItemResponse] = []       # 关联的项目明细列表
    rule_hits: List[RuleHitItem] = []           # 规则命中提示(提交/重新提交时生成)
    flow_id: int | None = None                  # 所用审批流ID
    current_level: int = 1                      # 当前审批级别
    approval_chain: list[str] | None = None     # 审批链(后端生成)

    # 允许 Pydantic 模型直接从 ORM 对象（SQLAlchemy 的数据库模型实例）读取属性，自动完成字段转换，不需要手动把数据库对象拆成字典
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性


# 报销单列表 --- 分页式响应
class ExpenseListResponse(BaseModel):
    """
    报销单分页响应模型（「我的 / 本部门 / 全公司」三个列表接口的统一返回外壳）
        :ivar items: 当前页的报销单列表
        :ivar total: 符合条件的总条数
        :ivar page: 当前页码，从 1 开始
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[ExpenseResponse] = []      # 当前页的报销单列表
    total: int                             # 符合条件的总条数
    page: int                              # 当前页码(从1开始)
    page_size: int                         # 每页条数
    total_pages: int                       # 总页数


# 待审批报销单的列表项
class PendingApprovalItem(BaseModel):
    """
    审批待办列表项模型（审批工作台用，只返回列表展示必需的精简字段）
        与 ExpenseResponse 相比刻意不返回完整明细、AI 审核结果等大字段，
        但会带回"规则风险标记"：审批人需要一眼看出"这单有什么风险、风险在哪条明细"。
        :ivar id: 报销单ID，点击进入详情/发起审批时使用
        :ivar expense_no: 报销单号
        :ivar title: 报销标题
        :ivar total_amount: 报销总金额
        :ivar status: 单据状态（待办场景下恒为 pending）
        :ivar current_level: 当前审批层级，前端可提示"第几级"
        :ivar created_at: 创建时间
        :ivar rule_risk_level: 本单规则风险整体级别：ok / warn / review / block（取最高）
        :ivar rule_risk_count: 命中规则条数（一条明细命中一次算一条）
        :ivar rule_risk_labels: 去重后的风险短标签，如 ["明细超类别限额", "发票缺失"]，用于列表徽标
        :ivar rule_risk_marks: 完整风险标记（含具体提示、关联明细、证据），展开/悬停时展示
    """
    id: int                         # 报销单ID
    expense_no: str                 # 报销单号
    title: str                      # 报销标题
    total_amount: Decimal           # 总金额
    status: ExpenseStatus           # 单据状态
    current_level: int              # 当前审批层级
    created_at: datetime            # 创建时间
    rule_risk_level: str = "ok"                     # 规则风险整体级别
    rule_risk_count: int = 0                        # 命中条数
    rule_risk_labels: List[str] = []                # 风险短标签(去重)
    rule_risk_marks: List[RuleRiskMark] = []        # 完整风险标记

    # 允许 Pydantic 模型直接从 ORM 对象（SQLAlchemy 的数据库模型实例）读取属性，自动完成字段转换，不需要手动把数据库对象拆成字典
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性



# 待审批报销单列表 --- 分页式响应
class PendingApprovalListResponse(BaseModel):
    """
    审批待办分页响应模型（待办列表与已办列表共用同一外壳）
        与报销单列表分页结构保持一致，前端可用同一套分页组件渲染，仅列表项字段为精简的待办项。
        :ivar items: 当前页待办（或已办）数据
        :ivar total: 权限过滤后的总条数
        :ivar page: 当前页码，从 1 开始
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[PendingApprovalItem] = []   # 当前页待办数据
    total: int                              # 总待办条数
    page: int                               # 当前页码
    page_size: int                          # 每页条数
    total_pages: int                        # 总页数



#  报销状态枚举选项
class DictOption(BaseModel):
    """
    通用字典选项响应模型（所有枚举下拉的统一结构）
        费用大类、报销状态、审批结果、用户角色等字典接口都返回 `[{value, label}, ...]`，
        前端只需一套渲染逻辑即可适配全部下拉框与标签。
        :ivar value: 后端存储/参与查询的枚举值，如 "pending"
        :ivar label: 前端展示的中文名称，如 "待审批"
    """
    value: str          # 后端存储的枚举值
    label: str          # 前端显示的标签名




# ============ 费用类别模式 ============
# 费用类别分类响应
class CategoryResponse(BaseModel):
    """
    费用分类（小类）出参模型（费用分类字典接口的返回结构，与 categories 表字段对齐）
        :ivar id: 费用类别ID，明细提交时作为 category_id 使用
        :ivar name: 类别名称，如"高铁票"
        :ivar code: 类别编码，全局唯一，便于程序判断与外部系统对接
        :ivar max_amount: 该类别单次报销限额，null 表示不限额
        :ivar expense_type: 所属费用性质大类，前端可据此做"大类 → 小类"级联
        :ivar description: 类别说明
    """
    id: int                                 # 费用类别ID
    name: str                               # 费用类别名称,如"差旅费"
    code: str                               # 费用类别代码,如"travel_bus",唯一,便于程序识别
    max_amount: Optional[Decimal] = None    # 该类别单次报销最高限额,为空表示不限额
    expense_type: ExpenseType               # 所属费用性质大类
    description: Optional[str] = None       # 费用类别描述

    # 允许 Pydantic 模型直接从 ORM 对象（SQLAlchemy 的数据库模型实例）读取属性，自动完成字段转换，不需要手动把数据库对象拆成字典
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性
