"""
规则服务（管理端规则维护的业务规则）
    规则表 rules 有两类用途，配置校验规则不同：
        - amount（路由规则）：按「角色 + 金额区间」决定走哪条审批流，写错会让提交 500 或走错流程；
        - check_*（检查规则）：产出风险提示给审批人，不改变审批流，写错只影响提示准确性。
    因此管理端写操作要守住：
        - 类型：只允许下面白名单里的 rule_type，其他类型引擎不会执行，直接拒绝；
        - amount：submitter_roles 合法且非空、min_amount >= 0、max_amount 为空表示不设上限、
          flow_id 必须指向"存在且启用"的流程、同角色的启用规则金额区间不能重叠
          （匹配是"第一条命中即生效"，重叠会让后面的规则永远命中不到）；
        - check_*：action 只能是 warn / block / review，config 里只保留引擎认识的参数
          （check_duplicate.window_days、check_period_late.max_days、
           check_evidence_strength 的弱/中等凭证金额阈值）；
          其中 check_evidence_strength 由 AI 预审阶段的 document_service 执行，不在提交时执行；
        - 名称唯一：避免管理员选错规则。
    约定：写操作直接 commit，失败抛 HTTPException。
"""
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models import Rule, ApprovalFlow, UserRole
from app.models.expense import ExpenseType



# 引擎支持的规则类型（amount 路由 + 四种检查）
SUPPORTED_RULE_TYPES = (                        # 支持的规则类型
    "amount",
    "check_category_limit",
    "check_invoice_missing",
    "check_duplicate",
    "check_period_late",
    "check_evidence_strength",
)

# 检查型规则的提示级别
CHECK_LEVELS = ("warn", "review", "block")      # 检查规则级别



# 取出所有合法角色值
def _allowed_roles() -> set[str]:
    """
    取出系统支持的全部角色字符串（与 UserRole 枚举保持一致）
        :return: 角色值集合，如 {"admin","boss","finance","manager","employee"}
    """
    return {role.value for role in UserRole}



# 校验金额区间是否相交
def _ranges_intersect(a_min, a_max, b_min, b_max) -> bool:
    """
    判断两个半开上限区间 [a_min, a_max] 与 [b_min, b_max] 是否相交（None 表示无穷大）
        :param a_min: 规则A下限
        :param a_max: 规则A上限，None 表示无上限
        :param b_min: 规则B下限
        :param b_max: 规则B上限，None 表示无上限
        :return: 相交返回 True
    """
    return (a_max is None or b_min <= a_max) and (b_max is None or a_min <= b_max)



# 金额区间重叠校验
def ensure_no_amount_overlap(
    db: Session,                                    # 数据库会话
    *,
    roles: list[str],                               # 提交人角色
    min_amount: float,                              # 金额下限
    max_amount: float | None,                       # 金额上限
    exclude_rule_id: int | None = None,             # 排除的规则ID
) -> None:
    """
    校验新规则与"启用中的金额规则"是否存在角色与金额区间的重叠
        只在规则本身启用时调用：停用的规则不参与匹配，允许先建草稿再启用。
        :param db: 数据库会话
        :param roles: 本规则的提交人角色列表
        :param min_amount: 本规则金额下限
        :param max_amount: 本规则金额上限，None 表示无上限
        :param exclude_rule_id: 修改时排除自己的ID
        :raises HTTPException: 400 与某条启用规则重叠
    """
    role_set = set(roles)                           # 角色集合

    for rule in db.query(Rule).filter(Rule.rule_type == "amount", Rule.is_active == True).all():
        # 1.修改自己时跳过自己
        if exclude_rule_id is not None and rule.id == exclude_rule_id:
            continue

        config = rule.config or {}                  # 规则配置
        other_roles = set(config.get("submitter_roles") or [])   # 其他规则角色

        # 2.角色没有交集，两条规则作用于不同人群，不算冲突
        if not (role_set & other_roles):
            continue

        other_min = config.get("min_amount", 0) or 0   # 其他规则下限
        other_max = config.get("max_amount")           # 其他规则上限

        # 3.区间相交 → 拒绝，并指出是哪条规则
        if _ranges_intersect(min_amount, max_amount, other_min, other_max):
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"与启用中的规则「{rule.name}」金额区间重叠"   # 错误提示
                       f"(角色交集: {'/'.join(sorted(role_set & other_roles))}),"
                       f"匹配是第一条命中即生效,请先停用或调整该规则",
            )



# 校验并规范化 amount（路由）规则配置
def _normalize_amount_config(db: Session, config: dict) -> dict:
    """
    校验并规范化 amount（路由）规则的配置
        amount 类型要求：
            - submitter_roles：非空数组，取值必须是合法用户角色；
            - min_amount：>= 0 的数字；
            - max_amount：null 表示无上限，否则必须大于 min_amount；
            - flow_id：必须指向存在且启用的审批流。
        :param db: 数据库会话
        :param config: 前端传入的配置字典
        :return: 规范化后的配置（只保留引擎认识的四个键）
        :raises HTTPException: 400 配置不合法
    """
    # 1.提交人角色
    roles = config.get("submitter_roles")           # 提交人角色
    if not isinstance(roles, list) or not roles:
        raise HTTPException(status_code=400, detail="config.submitter_roles 必须是非空数组")
    allowed = _allowed_roles()                      # 合法角色集
    invalid_roles = [role for role in roles if role not in allowed]   # 非法角色
    if invalid_roles:
        raise HTTPException(
            status_code=400,                        # 参数不合法
            detail=f"submitter_roles 含非法角色: {', '.join(map(str, invalid_roles))},"   # 错误提示
                   f"合法取值: {'/'.join(sorted(allowed))}",
        )

    # 2.金额下限
    min_amount = config.get("min_amount", 0)        # 金额下限
    try:
        min_value = float(min_amount)               # 下限数值
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="config.min_amount 必须是数字")
    if min_value < 0:
        raise HTTPException(status_code=400, detail="config.min_amount 不能为负数")

    # 3.金额上限：允许为空（表示不设上限）
    max_amount = config.get("max_amount")           # 金额上限
    max_value = None                                # 上限数值
    if max_amount is not None:
        try:
            max_value = float(max_amount)           # 上限数值
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="config.max_amount 必须是数字或 null")
        if max_value <= min_value:
            raise HTTPException(status_code=400, detail="config.max_amount 必须大于 min_amount")

    # 4.目标审批流必须存在且启用
    flow_id = config.get("flow_id")                 # 目标审批流ID
    if flow_id is None:
        raise HTTPException(status_code=400, detail="config.flow_id 不能为空")
    flow = db.query(ApprovalFlow).filter(ApprovalFlow.id == flow_id).first()   # 目标审批流
    if flow is None:
        raise HTTPException(status_code=400, detail="config.flow_id 指向的审批流不存在")
    if not flow.is_active:
        raise HTTPException(status_code=400, detail="config.flow_id 指向的审批流已停用,请先启用该流程")

    return {
        "submitter_roles": list(roles),     # 提交人角色列表
        "min_amount": min_amount,           # 金额下限
        "max_amount": max_amount,           # 金额上限(null 表示不设上限)
        "flow_id": flow.id,                 # 目标审批流ID
    }



# 读取并校验整数参数（天数、窗口大小）
def _int_param(config: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    """
    从 config 读取一个整数参数并做范围校验
        :param config: 规则配置
        :param key: 参数名，如 window_days
        :param default: 缺省值
        :param minimum: 允许的最小值
        :param maximum: 允许的最大值
        :return: 校验通过的整数
        :raises HTTPException: 400 不是整数或超出范围
    """
    # 1.取配置值（没配就用调用方给的默认值）
    value = config.get(key, default)                # 配置值
    # 2.转整数：失败说明配置类型不对
    try:
        number = int(value)                         # 整数值
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"config.{key} 必须是整数")
    # 3.范围校验：防止填 0 天或几万天这种无意义阈值
    if number < minimum or number > maximum:
        raise HTTPException(
            status_code=400,                        # 参数不合法
            detail=f"config.{key} 必须在 {minimum}~{maximum} 之间",   # 错误提示
        )
    # 4.返回校验通过的整数
    return number



# 读取浮点参数并做范围校验
def _float_param(config: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    """
    从 config 读取一个浮点参数并做范围校验
        :param config: 规则配置
        :param key: 参数名
        :param default: 缺省值
        :param minimum: 允许的最小值
        :param maximum: 允许的最大值
        :return: 校验通过的浮点数
        :raises HTTPException: 400 不是数字或超出范围
    """
    value = config.get(key, default)                # 配置值
    try:
        number = float(value)                       # 转浮点
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"config.{key} 必须是数字")
    if number < minimum or number > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"config.{key} 必须在 {minimum}~{maximum} 之间",
        )
    return number



# 校验凭证强度规则配置
def _normalize_evidence_config(config: dict) -> dict:
    """
    校验并规范化 check_evidence_strength 的配置
        结构：
            weak_max_amount：弱凭证免复核上限，默认 200；
            medium_max_amount：中等凭证免复核上限，默认 1000，必须 >= weak；
            category_overrides：按费用大类覆盖弱凭证上限，如 {"transportation": 300}。
        :param config: 前端传入的配置
        :return: 规范化后的配置
        :raises HTTPException: 400 配置不合法
    """
    weak = _float_param(config, "weak_max_amount", 200.0, 0.0, 1_000_000.0)   # 弱凭证上限
    medium = _float_param(config, "medium_max_amount", 1000.0, weak, 1_000_000.0)   # 中等凭证上限

    raw_overrides = config.get("category_overrides") or {}   # 类别覆盖
    if not isinstance(raw_overrides, dict):
        raise HTTPException(status_code=400, detail="config.category_overrides 必须是对象")

    allowed_types = {item.value for item in ExpenseType}     # 合法费用大类
    overrides: dict[str, float] = {}                         # 清洗后的覆盖
    for key, value in raw_overrides.items():
        expense_type = str(key).strip()                      # 费用大类
        if expense_type not in allowed_types:
            raise HTTPException(status_code=400, detail=f"config.category_overrides 键非法: {expense_type}")
        try:
            threshold = float(value)                          # 阈值
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"config.category_overrides.{expense_type} 必须是数字")
        if threshold < 0 or threshold > 1_000_000:
            raise HTTPException(status_code=400, detail=f"config.category_overrides.{expense_type} 超出范围")
        overrides[expense_type] = threshold

    return {
        "weak_max_amount": weak,                # 弱凭证上限
        "medium_max_amount": medium,            # 中等凭证上限
        "category_overrides": overrides,        # 类别覆盖
    }



# 校验并规范化规则配置（按类型分发）
def normalize_config(db: Session, rule_type: str, config: dict) -> dict:
    """
    校验并规范化规则配置
        不同类型对应不同的配置结构与校验规则：
            - amount：交给 _normalize_amount_config（角色 + 金额区间 + 审批流）；
            - check_category_limit / check_invoice_missing：无需参数，统一返回 {}；
            - check_duplicate：只需 window_days（重复判定窗口，默认 60 天）；
            - check_period_late：只需 max_days（跨期判定天数，默认 60 天）。
        :param db: 数据库会话
        :param rule_type: 规则类型
        :param config: 前端传入的配置字典
        :return: 规范化后的配置
        :raises HTTPException: 400 类型不支持或配置不合法
    """
    if rule_type not in SUPPORTED_RULE_TYPES:
        raise HTTPException(
            status_code=400,                        # 参数不合法
            detail=f"不支持的 rule_type: {rule_type}，可选: {'/'.join(SUPPORTED_RULE_TYPES)}",   # 错误提示
        )

    config = config or {}                           # 配置兜底

    # 1.路由规则
    if rule_type == "amount":
        return _normalize_amount_config(db, config)

    # 2.无需参数的检查规则
    if rule_type in ("check_category_limit", "check_invoice_missing"):
        return {}

    # 3.需要窗口/天数参数的检查规则
    if rule_type == "check_duplicate":
        return {"window_days": _int_param(config, "window_days", 60, 1, 365)}
    if rule_type == "check_period_late":
        return {"max_days": _int_param(config, "max_days", 60, 1, 365)}
    if rule_type == "check_evidence_strength":
        return _normalize_evidence_config(config)

    return {}



# 校验规则动作
def validate_action(rule_type: str, action: str) -> str:
    """
    校验规则 action 字段
        - amount（路由规则）：固定 assign_chain（命中后绑定审批链）；
        - check_*（检查规则）：只能是 warn / review / block。
        :param rule_type: 规则类型
        :param action: 前端传入的动作
        :return: 规范化后的动作
        :raises HTTPException: 400 动作不合法
    """
    # 1.统一去空格 + 转小写，避免 "WARN" / " warn " 这类写法
    normalized = (action or "").strip().lower()     # 规范化动作
    # 2.路由规则：动作固定为 assign_chain
    if rule_type == "amount":
        if normalized != "assign_chain":
            raise HTTPException(status_code=400, detail="amount（路由）规则的 action 必须是 assign_chain")
        return normalized
    # 3.检查规则：动作只能是 warn / review / block
    if normalized not in CHECK_LEVELS:
        raise HTTPException(
            status_code=400,                        # 参数不合法
            detail=f"检查规则的 action 只能是 {'/'.join(CHECK_LEVELS)}（warn 提示/review 强制复核/block 阻止提交）",   # 错误提示
        )
    # 4.返回规范化后的动作
    return normalized



# 名称唯一性校验
def _ensure_name_unique(db: Session, name: str, exclude_rule_id: int | None = None) -> None:
    """
    校验规则名称唯一（可排除自己）
        :param db: 数据库会话
        :param name: 规则名称（已去空格）
        :param exclude_rule_id: 修改时排除自己的ID
        :raises HTTPException: 409 名称已存在
    """
    # 1.按名称查重
    query = db.query(Rule).filter(Rule.name == name)   # 查重查询
    # 2.修改场景排除自己，避免"改成原名"被判重复
    if exclude_rule_id is not None:
        query = query.filter(Rule.id != exclude_rule_id)   # 排除自己
    # 3.查到同名规则就报 409
    if query.first() is not None:
        raise HTTPException(status_code=409, detail="规则名称已存在")



# 新建规则
def create_rule(
    db: Session,                                    # 数据库会话
    *,
    name: str,                                      # 规则名称
    rule_type: str,                                 # 规则类型
    condition: str,                                 # 触发条件
    action: str,                                    # 命中动作
    config: dict,                                   # 规则配置
    is_active: bool,                                # 是否启用
) -> Rule:
    """
    新建一条规则
        :param db: 数据库会话
        :param name: 规则名称（唯一）
        :param rule_type: 规则类型，目前只支持 amount
        :param condition: 触发条件的可读描述
        :param action: 命中动作
        :param config: 规则配置
        :param is_active: 是否启用
        :return: 新建的规则实体
        :raises HTTPException: 400 配置不合法/区间重叠；409 名称已存在
    """
    # 1.名称兜底 + 唯一
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="规则名称不能为空")
    _ensure_name_unique(db, clean_name)

    # 2.动作与配置校验（按类型分别校验）
    normalized_action = validate_action(rule_type, action)          # 规范化动作
    normalized_config = normalize_config(db, rule_type, config or {})   # 规范化配置

    # 3.路由规则启用时才校验区间重叠（停用的规则不参与匹配；检查规则没有区间概念）
    if is_active and rule_type == "amount":
        ensure_no_amount_overlap(
            db,                                     # 数据库会话
            roles=normalized_config["submitter_roles"],     # 适用提交人角色
            min_amount=float(normalized_config["min_amount"]),  # 金额下限
            max_amount=None if normalized_config["max_amount"] is None else float(normalized_config["max_amount"]),  # 金额上限（None 表示无上限）
        )

    # 4.入库
    rule = Rule(                                    # 新建规则实体
        name=clean_name,                    # 规则名称（已去空格并查重）
        rule_type=rule_type,                # 规则类型
        condition=condition,                # 触发条件描述
        action=normalized_action,           # 校验并规范化后的命中动作
        config=normalized_config,           # 校验并规范化后的规则参数
        is_active=is_active,                # 是否启用
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    return rule



# 修改规则（PUT 全量语义）
def update_rule(
    db: Session,                                    # 数据库会话
    rule: Rule,                                     # 被修改的规则
    *,
    name: str,                                      # 新名称
    rule_type: str,                                 # 新类型
    condition: str,                                 # 新条件描述
    action: str,                                    # 新动作
    config: dict,                                   # 新配置
    is_active: bool,                                # 新启用状态
) -> Rule:
    """
    修改一条规则（名称 / 类型 / 条件描述 / 动作 / 配置 / 启停）
        注意：改成"启用"时会重新做区间重叠校验，避免通过"先停用、改区间、再启用"绕过冲突检查。
        :param db: 数据库会话
        :param rule: 被修改的规则实体
        :param name: 新名称
        :param rule_type: 新类型
        :param condition: 新条件描述
        :param action: 新动作
        :param config: 新配置
        :param is_active: 新启用状态
        :return: 修改后的规则实体
        :raises HTTPException: 400 配置不合法/区间重叠；409 名称重复
    """
    # 1.名称兜底 + 唯一（排除自己）
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="规则名称不能为空")
    _ensure_name_unique(db, clean_name, exclude_rule_id=rule.id)

    # 2.动作与配置校验（按类型分别校验）
    normalized_action = validate_action(rule_type, action)          # 规范化动作
    normalized_config = normalize_config(db, rule_type, config or {})   # 规范化配置

    # 3.路由规则启用时才校验区间重叠（检查规则没有区间概念）
    if is_active and rule_type == "amount":
        ensure_no_amount_overlap(
            db,                                     # 数据库会话
            roles=normalized_config["submitter_roles"],     # 适用提交人角色
            min_amount=float(normalized_config["min_amount"]),  # 金额下限
            max_amount=None if normalized_config["max_amount"] is None else float(normalized_config["max_amount"]),  # 金额上限（None 表示无上限）
            exclude_rule_id=rule.id,                # 修改时排除自己，避免"改成原区间"被判重叠
        )

    # 4.写回字段并提交
    rule.name = clean_name                          # 规则名称
    rule.rule_type = rule_type                      # 规则类型
    rule.condition = condition                      # 触发条件
    rule.action = normalized_action                 # 命中动作
    rule.config = normalized_config                 # 规则配置
    rule.is_active = is_active                      # 启用状态

    db.commit()
    db.refresh(rule)

    return rule



# 删除规则
def delete_rule(db: Session, rule: Rule) -> None:
    """
    删除一条规则
        规则不被任何表外键引用，删除后只是不再参与匹配；历史单据上已经绑定的审批流不受影响，
        因此这里不做引用校验（与审批流的删除限制不同）。
        :param db: 数据库会话
        :param rule: 待删除的规则实体
    """
    # 1.从会话删除规则
    db.delete(rule)
    # 2.提交事务，真正落库
    db.commit()
