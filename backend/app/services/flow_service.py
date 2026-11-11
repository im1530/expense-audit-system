"""
审批流服务（管理端审批流维护的业务规则）
    审批流是"模板"，被 rules.config.flow_id 引用、被 expenses.flow_id 绑定，因此写操作必须守住几条底线：
        - 层级必须从 1 开始连续递增：审批引擎按"当前层级 +1"找下一级、按 level 匹配当前级，
          跳号或重复会让流程卡死；
        - 角色只能是 manager / finance_other / finance / boss：
          admin 没有 approve 权限，出现在流程里会让单据永久卡在该级；
        - 名称唯一：避免管理员选错流程；
        - 默认流程唯一且不能直接停用：规则都不命中时靠它兜底，停掉会让提交直接 500；
        - 被规则引用或已被单据绑定的流程，不允许停用/删除。
    约定：写操作直接 commit，失败抛 HTTPException。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models import ApprovalFlow, Expense, Rule



# 审批流层级允许的角色（与 api/v1/approval.py 的资格判定保持一致）
ALLOWED_FLOW_ROLES = ("manager", "finance_other", "finance", "boss")   # 允许的审批角色



# 校验并规范化层级配置
def validate_levels(levels: list[dict]) -> list[dict]:
    """
    校验审批层级配置，并返回规范化的列表
        规则：
            - 至少一级；
            - 每项的 level 必须等于它在数组中的位置（1,2,3...），保证连续且唯一；
            - role 必须是 ALLOWED_FLOW_ROLES 之一，admin 明确禁止。
        :param levels: 前端传入的层级列表，形如 [{"level":1,"role":"manager"}]
        :return: 规范化后的层级列表（只保留 level / role 两个字段）
        :raises HTTPException: 400 层级为空、跳号、重复或角色不合法
    """
    # 1.至少一级
    if not levels:
        raise HTTPException(status_code=400, detail="审批层级不能为空,至少需要一级")

    normalized = []                                 # 规范化层级
    for index, item in enumerate(levels, start=1):
        # 2.每项必须是字典（Pydantic 已经保证,这里再兜一层,防止直接调用服务时传错）
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"第 {index} 级配置格式不正确")

        level = item.get("level")                   # 层级编号
        role = item.get("role")                     # 审批角色

        # 3.level 必须与位置一致：1、2、3... 不能跳号也不能重复
        if level != index:
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"第 {index} 级配置的 level 必须是 {index},请检查层级是否连续",   # 错误提示
            )

        # 4.角色必须是引擎认识的值；admin 会被显式拒绝并给出原因
        if role not in ALLOWED_FLOW_ROLES:
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"第 {index} 级角色不合法,只支持 {'/'.join(ALLOWED_FLOW_ROLES)}"   # 错误提示
                       f"(admin 没有审批权限,不能出现在审批流中)",
            )

        normalized.append({"level": index, "role": role})

    return normalized



# 统计流程被引用的次数
def count_flow_references(db: Session, flow_id: int) -> tuple[int, int]:
    """
    统计某个审批流被引用的次数
        - 单据引用：expenses.flow_id，说明已经有报销单在用这个流程，删除会让单据失去流程定义；
        - 规则引用：rules.config.flow_id，说明审批流匹配会指向它，删除会让提交时报"规则命中的审批流不存在"。
        :param db: 数据库会话
        :param flow_id: 审批流ID
        :return: (绑定该流程的单据数, 引用该流程的规则数)
    """
    # 1.单据引用数
    expense_count = db.query(func.count(Expense.id)).filter(Expense.flow_id == flow_id).scalar() or 0   # 单据引用数

    # 2.规则引用数：config 是 JSON，规则总量很小，取出来在内存里比对即可
    rule_count = 0                                  # 规则引用数
    for rule in db.query(Rule).all():
        config = rule.config or {}                  # 规则配置
        flow_ref = config.get("flow_id")            # 引用的流程ID
        if flow_ref is not None and int(flow_ref) == int(flow_id):
            rule_count += 1                         # 引用数加一

    return expense_count, rule_count



# 名称唯一性校验
def _ensure_name_unique(db: Session, name: str, exclude_flow_id: int | None = None) -> None:
    """
    校验流程名称唯一（可排除自己，供修改时使用）
        :param db: 数据库会话
        :param name: 待校验的名称（已去空格）
        :param exclude_flow_id: 修改时排除自己的ID
        :raises HTTPException: 409 名称已存在
    """
    # 1.按名称查重
    query = db.query(ApprovalFlow).filter(ApprovalFlow.name == name)   # 查重查询
    # 2.修改场景排除自己，避免"改成原名"被判重复
    if exclude_flow_id is not None:
        query = query.filter(ApprovalFlow.id != exclude_flow_id)       # 排除自己
    # 3.查到同名流程就报 409
    if query.first() is not None:
        raise HTTPException(status_code=409, detail="审批流名称已存在")



# 把其他流程的默认标记清掉（保证全表只有一个默认流程）
def _clear_other_defaults(db: Session, keep_flow_id: int | None = None) -> None:
    """
    清除其他审批流的默认标记
        数据库没有"唯一默认"约束，只能在写操作里保证：设置新默认流时把别人的 is_default 改 False。
        :param db: 数据库会话
        :param keep_flow_id: 保留默认标记的流程ID（修改自己时用）
    """
    # 1.查出当前所有默认流程
    query = db.query(ApprovalFlow).filter(ApprovalFlow.is_default == True)   # 默认流查询
    # 2.修改自己时要把自己排除，否则会把自己的默认标记也清掉
    if keep_flow_id is not None:
        query = query.filter(ApprovalFlow.id != keep_flow_id)              # 保留自己
    # 3.批量置为 False（保证全表至多一个默认流）
    query.update({"is_default": False})



# 新建审批流
def create_flow(
    db: Session,                                    # 数据库会话
    *,
    name: str,                                      # 流程名称
    description: str | None,                        # 流程说明
    levels: list[dict],                             # 层级配置
    is_active: bool,                                # 是否启用
    is_default: bool,                               # 是否默认流程
) -> ApprovalFlow:
    """
    新建一个审批流
        :param db: 数据库会话
        :param name: 流程名称（唯一）
        :param description: 流程说明，可空
        :param levels: 层级配置（会先做连续性与角色校验）
        :param is_active: 是否启用
        :param is_default: 是否设为默认流程（会清掉其他流程的默认标记）
        :return: 新建的审批流实体
        :raises HTTPException: 400 层级不合法；409 名称已存在
    """
    # 1.名称兜底 + 唯一
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="审批流名称不能为空")
    _ensure_name_unique(db, clean_name)

    # 2.层级校验
    normalized_levels = validate_levels(levels)     # 规范化层级

    # 3.默认流程唯一
    if is_default:
        _clear_other_defaults(db)

    # 4.入库
    flow = ApprovalFlow(                            # 新建流程实体
        name=clean_name,                # 流程名称（已去空格并查重）
        description=description,        # 流程说明
        levels=normalized_levels,       # 校验并规范化后的层级配置
        is_active=is_active,            # 是否启用
        is_default=is_default,          # 是否设为默认流程
    )
    db.add(flow)
    db.commit()
    db.refresh(flow)

    return flow



# 修改审批流（PUT 全量语义）
def update_flow(
    db: Session,                                    # 数据库会话
    flow: ApprovalFlow,                             # 被修改的流程实体
    *,
    name: str,                                      # 新名称
    description: str | None,                        # 新说明
    levels: list[dict],                             # 新层级配置
    is_active: bool,                                # 新启用状态
    is_default: bool,                               # 新默认标记
) -> ApprovalFlow:
    """
    修改一个审批流（名称 / 说明 / 层级 / 启停 / 默认标记）
        停用保护（按顺序判断）：
            1. 仍是默认流程时不允许停用——规则都不命中时靠它兜底；
            2. 被启用中的规则引用时不允许停用——停用后提交会直接 500；
            3. 被单据绑定时允许停用（历史单据已绑定，停用只影响后续匹配），但会禁止删除。
        :param db: 数据库会话
        :param flow: 被修改的流程实体
        :param name: 新名称
        :param description: 新说明
        :param levels: 新层级配置
        :param is_active: 新启用状态
        :param is_default: 新默认标记（true 时清掉其他流程的默认标记）
        :return: 修改后的流程实体
        :raises HTTPException: 400 规则冲突；409 名称重复
    """
    # 1.名称兜底 + 唯一（排除自己）
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="审批流名称不能为空")
    _ensure_name_unique(db, clean_name, exclude_flow_id=flow.id)

    # 2.层级校验
    normalized_levels = validate_levels(levels)     # 规范化层级

    # 3.停用/默认标记保护
    if not is_active:
        # 3.1 默认流程不能直接停用
        if is_default:
            raise HTTPException(status_code=400, detail="默认审批流必须保持启用")
        # 3.2 被启用规则引用时不能停用
        rule_count = 0                              # 引用规则数
        for rule in db.query(Rule).filter(Rule.is_active == True).all():
            flow_ref = (rule.config or {}).get("flow_id")   # 引用的流程ID
            if flow_ref is not None and int(flow_ref) == int(flow.id):
                rule_count += 1                     # 引用数加一
        if rule_count > 0:
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"该流程被 {rule_count} 条启用中的规则引用,请先调整规则后再停用",   # 错误提示
            )

    # 3.3 不能直接把唯一的默认流程"取消默认",否则规则都不命中时没有兜底流程
    # 正确做法：直接设置另一条流程为默认，系统会自动清掉这一条的默认标记
    if flow.is_default and not is_default:
        raise HTTPException(
            status_code=400,                        # 参数不合法
            detail="默认审批流不能取消默认,请直接设置另一条流程为默认",   # 错误提示
        )

    # 4.默认流程唯一
    if is_default:
        _clear_other_defaults(db, keep_flow_id=flow.id)

    # 5.写回字段并提交
    flow.name = clean_name                          # 流程名称
    flow.description = description                  # 流程说明
    flow.levels = normalized_levels                 # 层级配置
    flow.is_active = is_active                      # 启用状态
    flow.is_default = is_default                    # 默认标记

    db.commit()
    db.refresh(flow)

    return flow



# 删除审批流
def delete_flow(db: Session, flow: ApprovalFlow) -> None:
    """
    删除一个审批流（仅允许删除"完全没被使用"的流程）
        为什么限制这么严：
            - 被单据引用：删掉后报销单的 flow_id 变成悬空外键（虽然数据库会拦，但错误信息不可读）；
            - 被规则引用：删掉后 match_approval_flow 会抛"规则命中的审批流不存在或已停用"。
        真的不需要了，优先用"停用"；停用不影响历史单据，只是不再被新单据匹配到。
        :param db: 数据库会话
        :param flow: 待删除的流程实体
        :raises HTTPException: 409 仍被单据或规则引用
    """
    expense_count, rule_count = count_flow_references(db, flow.id)   # 单据与规则引用数

    # 1.被单据引用 → 禁止删除
    if expense_count > 0:
        raise HTTPException(
            status_code=409,                        # 资源冲突
            detail=f"该流程已被 {expense_count} 张报销单使用,不能删除,可以改为停用",   # 错误提示
        )

    # 2.被规则引用 → 禁止删除
    if rule_count > 0:
        raise HTTPException(
            status_code=409,                        # 资源冲突
            detail=f"该流程被 {rule_count} 条规则引用,请先修改或删除这些规则",   # 错误提示
        )

    # 3.默认流程即使没被引用也不能删,否则规则兜底失效
    if flow.is_default:
        raise HTTPException(status_code=409, detail="默认审批流不能删除,请先指定其他流程为默认")

    # 4.删除并提交
    db.delete(flow)
    db.commit()
