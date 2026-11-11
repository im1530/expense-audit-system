"""
制度知识库数据结构（Phase B：结构化条款 + 关键词/BM25 检索）
    设计目标：
        1. 条款是检索单元，也是引用单元：clause_id 稳定、text 逐字保留；
        2. 条款元数据承载过滤条件（费用大类/角色/金额区间/规则类型），检索前先过滤；
        3. 检索结果与 Prompt 注入使用同一份数据，避免"检索到了但没给模型看"；
        4. 本模块只定义纯数据结构，不读文件、不查库、不调用 LLM。
    与未来向量检索的关系：
        - PolicyClause 同时可以作为 Embedding 的文档单元，换检索实现时不必重新切块；
        - PolicyRetriever 协议在 retriever.py 中定义，本文件不关心具体实现。
"""
from dataclasses import dataclass
from decimal import Decimal



# 费用大类白名单：与 ExpenseType 枚举保持一致
ALLOWED_EXPENSE_TYPES = (
    "meal",             # 餐饮费
    "transportation",   # 交通费
    "accommodation",    # 住宿费
    "office",           # 办公费
    "other",            # 其他
)

# 用户角色白名单：与 UserRole 枚举保持一致
ALLOWED_ROLES = (
    "admin",        # 系统管理员
    "boss",         # 老板
    "finance",      # 财务
    "manager",      # 部门经理
    "employee",     # 普通员工
)

# 规则类型白名单：与 rules.rule_type 的取值保持一致
ALLOWED_RULE_TYPES = (
    "amount",                   # 金额类规则（匹配审批流）
    "check_category_limit",     # 类别单次限额
    "check_invoice_missing",    # 发票缺失
    "check_duplicate",          # 重复报销
    "check_period_late",        # 跨期报销
)



# 一条结构化制度条款
@dataclass(frozen=True)
class PolicyClause:
    """
    制度条款（检索与引用的最小单元）
        :ivar clause_id: 条款编号，如 5.2.1，全局唯一且稳定
        :ivar section: 所属章节标题，如 "5.2 交通费"
        :ivar text: 条款原文，逐字保留，引用校验的唯一依据
        :ivar keywords: 人工维护的检索关键词/同义词
        :ivar expense_types: 适用的费用大类；空元组表示不限
        :ivar roles: 适用的提交人角色；空元组表示不限
        :ivar rule_types: 适用的规则类型；空元组表示不限
        :ivar min_amount: 适用金额下限（按单据总额），None 表示不限
        :ivar max_amount: 适用金额上限（按单据总额），None 表示不限
        :ivar version: 条款所属制度版本
    """
    clause_id: str                                  # 条款编号
    section: str                                    # 所属章节
    text: str                                       # 条款原文
    keywords: tuple[str, ...] = ()                  # 检索关键词
    expense_types: tuple[str, ...] = ()             # 适用费用大类
    roles: tuple[str, ...] = ()                     # 适用角色
    rule_types: tuple[str, ...] = ()                # 适用规则类型
    min_amount: Decimal | None = None               # 适用金额下限
    max_amount: Decimal | None = None               # 适用金额上限
    version: str = ""                               # 制度版本

    @property
    def label(self) -> str:
        """
        生成人类可读的条款标签
            :return: 形如 "5.2 交通费 5.2.1"
        """
        return f"{self.section} {self.clause_id}".strip()   # 章节 + 编号



# 一份完整制度文档
@dataclass(frozen=True)
class PolicyDocument:
    """
    解析后的制度文档
        :ivar doc_id: 制度标识，如 expense_policy
        :ivar title: 制度名称，如 费用报销管理制度
        :ivar version: 制度版本，如 v1.0
        :ivar clauses: 全部条款，按文档顺序排列
        :ivar source_path: 源文件路径（排障与审计用）
    """
    doc_id: str                                     # 制度标识
    title: str                                      # 制度名称
    version: str                                    # 制度版本
    clauses: tuple[PolicyClause, ...] = ()          # 条款列表
    source_path: str = ""                           # 源文件路径



# 一次检索的查询条件
@dataclass(frozen=True)
class PolicyQuery:
    """
    制度检索查询（由报销单上下文构造）
        :ivar text: 查询文本（标题、明细说明、类别、规则标签等）
        :ivar expense_types: 本次单据涉及的费用大类集合
        :ivar role: 提交人角色；空串表示不限
        :ivar total_amount: 单据总金额，用于金额区间过滤
        :ivar rule_types: 本次命中的规则类型集合
    """
    text: str                                                       # 查询文本
    expense_types: frozenset[str] = frozenset()                     # 费用大类集合
    role: str = ""                                                  # 提交人角色
    total_amount: Decimal = Decimal("0")                            # 单据总金额
    rule_types: frozenset[str] = frozenset()                        # 规则类型集合



# 一条检索命中
@dataclass(frozen=True)
class PolicyHit:
    """
    单条制度检索结果
        :ivar clause: 命中的条款
        :ivar score: BM25 分数
        :ivar matched_terms: 命中的查询词（审计与排障用）
    """
    clause: PolicyClause                            # 命中条款
    score: float                                    # BM25 分数
    matched_terms: tuple[str, ...] = ()             # 命中词



# 一次检索的完整结果
@dataclass(frozen=True)
class PolicyBundle:
    """
    注入 Prompt 与引用校验共用的制度结果集
        :ivar doc_id: 制度标识
        :ivar doc_title: 制度名称
        :ivar version: 制度版本
        :ivar hits: 命中的条款列表（已按分数降序）
    """
    doc_id: str = ""                                # 制度标识
    doc_title: str = ""                             # 制度名称
    version: str = ""                               # 制度版本
    hits: tuple[PolicyHit, ...] = ()                # 命中条款

    @classmethod
    def empty(cls) -> "PolicyBundle":
        """
        构造一个"没有检索到制度"的空结果
            :return: 空 PolicyBundle
        """
        return cls()                                # 全部字段取默认值

    @property
    def is_empty(self) -> bool:
        """
        是否为空结果
            :return: 没有任何命中时返回 True
        """
        return not self.hits                        # 无命中即空

    @property
    def clause_ids(self) -> tuple[str, ...]:
        """
        本次检索到的全部条款编号
            :return: 按命中顺序排列的编号元组
        """
        return tuple(hit.clause.clause_id for hit in self.hits)   # 逐个取编号

    def limit_chars(self, max_chars: int) -> "PolicyBundle":
        """
        按字符预算裁剪条款，保证 Prompt 注入与引用校验使用同一批条款
            预算按"条款正文 + 编号 + 章节 + 固定开销"粗略计算；
            至少保留一条，避免因为预算过小把所有制度都裁掉。
            :param max_chars: 最大字符数
            :return: 裁剪后的新 PolicyBundle
        """
        # 1.预算非正数：直接返回空结果
        if max_chars <= 0:
            return PolicyBundle.empty()

        # 2.逐条累加，超预算即停止
        selected = []                                   # 已选中的命中
        used = 0                                        # 已用字符数
        for hit in self.hits:
            cost = len(hit.clause.text) + len(hit.clause.clause_id) + len(hit.clause.section) + 20   # 单条开销
            if selected and used + cost > max_chars:
                break                                   # 超预算，停止
            selected.append(hit)                        # 收入当前条款
            used += cost                                # 累计字符数

        # 3.返回裁剪后的结果（保留文档元信息）
        return PolicyBundle(
            doc_id=self.doc_id,                         # 制度标识
            doc_title=self.doc_title,                   # 制度名称
            version=self.version,                       # 制度版本
            hits=tuple(selected),                       # 选中的条款
        )
