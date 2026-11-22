"""
管理端-规则管理路由（挂载前缀 /api/v1/admin/rule）
    为什么需要：规则现在只能连数据库改，联调时想调整金额区间/绑定的审批流非常痛苦。
    当前提供的接口（同一前缀下）：
        - GET  ""      分页列表（可按类型、启用状态、关键字筛选）
        - GET  "/hit-stats" 规则命中率统计（按规则看命中次数/涉及单据数/命中率）
        - GET  "/{id}" 详情
        - POST ""      新建规则
        - PUT  "/{id}" 全量修改
        - DELETE "/{id}" 删除规则（规则没有外键引用，删除即不再参与匹配）
    权限约定：统一用 has_permission("manage_user") 判断，目前只有 admin 拥有。
    路由顺序注意："/hit-stats" 必须声明在 "/{rule_id}" 之前，
    否则会被当成 rule_id="hit-stats" 匹配到详情接口（422）。
"""



from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user               # 当前登录用户依赖
from app.models import User, Rule, ApprovalFlow         # 规则/审批流模型
from app.schemas.rule import (                          # 入参/出参
    AdminRuleCreate,
    AdminRuleItem,
    AdminRuleListResponse,
    AdminRuleUpdate,
    RuleHitStatsResponse,
)
from app.schemas.common import PageParams               # 通用分页参数
from app.services.rule_service import (                 # 规则规则(类型/角色/金额/审批流/区间重叠)
    create_rule,
    delete_rule,
    update_rule,
)
from app.services.rule_risk_service import get_rule_hit_stats   # 规则命中率统计
from app.utils.pagination import paginate_query, assemble_paginated_result



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 组装规则出参
def _build_rule_item(rule: Rule, flow_map: dict) -> dict:
    """
    把规则实体组装成管理端出参（列表、详情、新建、修改四处共用）
        额外解析出"这条规则指向哪个审批流、该流程是否启用"：
        流程被停用后，规则仍然存在但提交时会直接报错，前端需要能一眼看出来。
        :param rule: 规则实体
        :param flow_map: {流程ID: 流程实体} 的字典，由调用方批量查好传进来
        :return: 可直接作为 AdminRuleItem 出参的字典
    """
    # 1.解析规则配置里的目标审批流ID（只有 amount 类型规则才有 flow_id）
    config = rule.config or {}  # 规则配置
    flow_id = config.get("flow_id")  # 目标流程ID
    # 2.从预加载的字典里取流程实体；取不到说明流程已被删除
    flow = flow_map.get(int(flow_id)) if flow_id is not None else None  # 目标流程

    # 3.组装出参：规则字段 + 目标审批流名称/启用状态
    return {
        "id": rule.id,                                  # 规则ID
        "name": rule.name,                              # 规则名称
        "rule_type": rule.rule_type,                    # 规则类型
        "condition": rule.condition,                    # 触发条件描述
        "action": rule.action,                          # 引擎动作
        "severity": rule.severity,                      # 检查规则风险级别
        "config": rule.config,                          # 规则参数
        "is_active": rule.is_active,                    # 是否启用
        "flow_id": int(flow_id) if flow_id is not None else None,  # 目标审批流ID
        "flow_name": flow.name if flow is not None else None,      # 目标审批流名称
        "flow_is_active": flow.is_active if flow is not None else None,  # 目标审批流是否启用
        "created_at": rule.created_at,                  # 创建时间
        "updated_at": rule.updated_at,                  # 更新时间
    }



# 批量查出规则指向的审批流
def _load_flow_map(db: Session, rules: list[Rule]) -> dict:
    """
    按一批规则里出现的 flow_id，一次性查出对应的审批流
        :param db: 数据库会话
        :param rules: 规则列表
        :return: {流程ID: 流程实体} 字典（查不到的不在字典里）
    """
    # 1.收集这一批规则里出现的 flow_id（去重，避免重复查库）
    flow_ids = set()  # 流程ID集合
    for rule in rules:
        flow_id = (rule.config or {}).get("flow_id")  # 规则流程ID
        if flow_id is not None:
            flow_ids.add(int(flow_id))

    # 2.没有任何规则引用流程时，直接返回空字典
    if not flow_ids:
        return {}
    # 3.一次性查出所有流程，做成 {流程ID: 实体} 字典供组装时直接取
    return {
        flow.id: flow
        for flow in db.query(ApprovalFlow).filter(ApprovalFlow.id.in_(flow_ids)).all()
    }



# 分页查询规则列表的接口(仅admin可用)
@router.get(
    "",                                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule
    response_model=AdminRuleListResponse,   # 返回值按管理端分页响应模型格式化
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="分页查询规则列表(仅admin可用)",     # 接口标题
)
def list_rules(
    page_params: PageParams = Depends(),                                      # 分页参数
    rule_type: Optional[str] = Query(None, max_length=50, description="筛选:规则类型"),  # 规则类型
    is_active: Optional[bool] = Query(None, description="筛选:启用状态(true启用/false停用/不传则全部)"),  # 启用状态
    keyword: Optional[str] = Query(None, max_length=50, description="筛选:关键字,匹配规则名称/条件描述"),  # 搜索关键字
    db: Session = Depends(get_db),                                            # 注入数据库会话
    current_user: User = Depends(get_current_user),                           # 当前登录人（验签）
):
    """
    分页查询规则列表（管理端）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
    返回说明:
        - 每条规则都会带出目标审批流的名称与启用状态，便于发现"规则绑到了停用流程"的配置错误
    排序:
        - 按规则ID升序，与 match_approval_flow 的遍历顺序一致，避免列表顺序与实际生效顺序不一致
        :param page_params: 分页参数
        :param rule_type: 可选，按规则类型筛选
        :param is_active: 可选，按启用状态筛选
        :param keyword: 可选，模糊搜索规则名称或条件描述
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: AdminRuleListResponse 实例
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看规则列表")

    # 2.构建查询
    query = db.query(Rule)  # 规则查询

    # 3.追加筛选条件
    if rule_type:
        query = query.filter(Rule.rule_type == rule_type)  # 类型筛选
    if is_active is not None:
        query = query.filter(Rule.is_active == is_active)  # 状态筛选
    if keyword:
        kw = keyword.strip()  # 去空格关键字
        if kw:
            like = f"%{kw}%"  # 模糊匹配串
            query = query.filter(  # 名称或条件匹配
                Rule.name.like(like) | Rule.condition.like(like)
            )

    # 4.排序 + 分页
    query = query.order_by(Rule.id.asc())  # ID升序
    rules, total = paginate_query(  # 规则分页
        query=query,                            # 已加筛选与排序的查询对象
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size,        # 每页条数
    )

    # 5.批量查审批流并组装
    flow_map = _load_flow_map(db, rules)  # 流程映射
    result_items = [_build_rule_item(rule, flow_map) for rule in rules]  # 规则出参列表

    # 6.组装分页结果
    return assemble_paginated_result(
        items=result_items,                             # 当前页规则出参
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=AdminRuleListResponse,           # 响应模型
    )



# 规则命中率统计的接口(仅admin可用)
@router.get(
    "/hit-stats",                           # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule/hit-stats
    response_model=RuleHitStatsResponse,    # 返回值按命中统计响应模型格式化
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="规则命中率统计(仅admin可用)",     # 接口标题
)
def get_rule_hit_statistics(
    start_date: Optional[date] = Query(None, description="统计起始日期(按报销单提交时间)"),  # 统计起始日
    end_date: Optional[date] = Query(None, description="统计结束日期(含当天)"),  # 统计结束日
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    统计各检查规则在统计区间内的命中情况（管理端调阈值/停用规则的依据）
    指标口径:
        - submitted_total：区间内已提交的报销单数（submitted_at 非空），作为命中率分母；
        - 每条规则：hit_count（命中记录条数）、expense_count（涉及单据数，去重）、
          warn/review/block_count（各级别条数）、hit_rate（expense_count / submitted_total）；
        - 整体：hit_expense_count（至少命中一条规则的单据数）、overall_hit_rate、total_hits。
    说明:
        - 命中记录在每次提交/重新提交时整体替换，代表每张单最近一次检查的结果，不会重复累计历史；
        - amount 类型规则只参与审批流匹配、不产生检查命中，因此不出现在统计列表中；
        - 规则被删除后，其历史命中仍按名称快照单独列出（is_active=false），避免数据凭空消失。
        :param start_date: 可选，统计起始日期（按提交时间）
        :param end_date: 可选，统计结束日期（含当天）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 整体指标 + 逐条规则统计（按命中条数倒序）
        :raises HTTPException: 403 无权限；400 起始日期晚于结束日期
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看规则命中统计")

    # 2.日期区间合法性（空区间直接给 400，避免返回一份看起来正常的全 0 报表）
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=400, detail="起始日期不能晚于结束日期")

    # 3.统计并返回
    return get_rule_hit_stats(
        db,                                 # 数据库会话
        start_date=start_date,              # 统计起始日期（按提交时间）
        end_date=end_date,                  # 统计结束日期（含当天）
    )



# 查询规则详情的接口(仅admin可用)
@router.get(
    "/{rule_id}",                           # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule/{rule_id}
    response_model=AdminRuleItem,           # 单条规则出参
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="查询规则详情(仅admin可用)",        # 接口标题
)
def get_rule_detail(
    rule_id: int,                                                           # 路径参数，规则ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    查询单条规则详情（编辑页回填）
        :param rule_id: 路径参数，规则ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 规则详情（含目标审批流名称与启用状态）
        :raises HTTPException: 403 无权限；404 规则不存在
    """
    # 1.权限校验：仅 admin
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看规则详情")

    # 2.按ID查规则，不存在返回 404
    rule = db.query(Rule).filter(Rule.id == rule_id).first()  # 目标规则
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    # 3.组装出参（含目标审批流的名称与启用状态）
    return _build_rule_item(rule, _load_flow_map(db, [rule]))



# 新建规则的接口(仅admin可用)
@router.post(
    "",                             # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule
    response_model=AdminRuleItem,   # 返回新建的规则
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="新建规则(仅admin可用)",   # 接口标题
)
def create_new_rule(
    payload: AdminRuleCreate,                                               # 入参：名称/类型/条件/动作/配置/启用
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    新建一条金额类审批规则
    校验规则（详见 services.rule_service）:
        - rule_type 目前只支持 amount
        - submitter_roles 必须是非空合法角色
        - min_amount ≥ 0，max_amount 为 null 表示无上限且必须大于 min_amount
        - config.flow_id 必须指向存在且启用的审批流
        - 启用状态下，与同角色的其他启用规则金额区间不能重叠（重叠会导致后面的规则永不生效）
        :param payload: 新建入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 新建的规则
        :raises HTTPException: 403 无权限；409 名称重复；400 配置不合法或区间重叠
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可新建规则")

    # 2.调用服务层创建（含全部业务校验）
    rule = create_rule(  # 新建规则
        db,                                     # 数据库会话
        name=payload.name,                      # 规则名称
        rule_type=payload.rule_type,            # 规则类型（amount / check_*）
        condition=payload.condition,            # 触发条件描述
        action=payload.action,                  # 引擎动作
        severity=payload.severity,              # 检查规则风险级别
        config=payload.config,                  # 规则参数（JSON）
        is_active=payload.is_active,            # 是否启用
    )

    # 3.返回（带上目标审批流信息）
    return _build_rule_item(rule, _load_flow_map(db, [rule]))



# 修改规则的接口(全量,仅admin可用)
@router.put(
    "/{rule_id}",                   # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule/{rule_id}
    response_model=AdminRuleItem,   # 返回修改后的规则
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="修改规则(全量,仅admin可用)",  # 接口标题
)
def update_existing_rule(
    rule_id: int,                                                           # 路径参数，规则ID
    payload: AdminRuleUpdate,                                               # 全量入参
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    修改指定规则（PUT 全量：名称/类型/条件/动作/配置/启用一次发全）
    注意:
        - 改成"启用"时会重新校验区间重叠，防止"先停用、改区间、再启用"绕过冲突检查
        - 修改对后续提交生效，历史上已绑定的单据不受影响
        :param rule_id: 路径参数，规则ID
        :param payload: 全量修改入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 修改后的规则
        :raises HTTPException: 403 无权限；404 规则不存在；409 名称重复；400 配置不合法或区间重叠
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可修改规则")

    # 2.查规则
    rule = db.query(Rule).filter(Rule.id == rule_id).first()  # 目标规则
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    # 3.调用服务层修改
    rule = update_rule(  # 修改后的规则
        db,                                     # 数据库会话
        rule,                                   # 待修改的规则实体
        name=payload.name,                      # 规则名称
        rule_type=payload.rule_type,            # 规则类型
        condition=payload.condition,            # 触发条件描述
        action=payload.action,                  # 引擎动作
        severity=payload.severity,              # 检查规则风险级别
        config=payload.config,                  # 规则参数（JSON）
        is_active=payload.is_active,            # 是否启用
    )

    # 4.返回
    return _build_rule_item(rule, _load_flow_map(db, [rule]))



# 删除规则的接口(仅admin可用)
@router.delete(
    "/{rule_id}",                   # 接口路径；拼上总前缀后完整为 /api/v1/admin/rule/{rule_id}
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="删除规则(仅admin可用)",   # 接口标题
)
def delete_existing_rule(
    rule_id: int,                                                           # 路径参数，规则ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    删除指定规则
    说明:
        - 规则没有被任何表外键引用，删除后只是不再参与匹配；
        - 历史单据已经绑定的审批流不受影响（匹配发生在提交那一刻）；
        - 如果只是暂时不想用，建议"停用"而不是删除，便于以后恢复。
        :param rule_id: 路径参数，规则ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 删除结果提示
        :raises HTTPException: 403 无权限；404 规则不存在
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可删除规则")

    # 2.查规则
    rule = db.query(Rule).filter(Rule.id == rule_id).first()  # 目标规则
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    # 3.删除并返回提示
    delete_rule(db, rule)
    return {"message": "规则已删除", "id": rule_id}
