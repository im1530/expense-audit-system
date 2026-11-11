"""
管理端-审批流管理路由（挂载前缀 /api/v1/admin/approval-flow）
    为什么需要：目前 8 套审批流只能连数据库改，想调整层级或新增流程非常麻烦。
    当前提供的接口（同一前缀下）：
        - GET  ""       分页列表（可按启用状态、关键字筛选，默认流程排在最前）
        - GET  "/{id}"  详情
        - POST ""       新建流程
        - PUT  "/{id}"  全量修改（名称/说明/层级/启停/默认标记）
        - DELETE "/{id}" 删除流程（仅允许删除完全没被单据或规则引用的流程）
    权限约定：统一用 has_permission("manage_user") 判断，目前只有 admin 拥有。
"""



from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user               # 当前登录用户依赖
from app.models import User, ApprovalFlow, Expense, Rule  # 用户/流程/单据/规则模型
from app.schemas.approval import (                      # 管理端入参/出参
    AdminFlowCreate,
    AdminFlowItem,
    AdminFlowListResponse,
    AdminFlowUpdate,
)
from app.schemas.common import PageParams               # 通用分页参数
from app.services.flow_service import (                 # 流程规则(层级校验/默认唯一/引用保护)
    count_flow_references,
    create_flow,
    delete_flow,
    update_flow,
)
from app.utils.pagination import paginate_query, assemble_paginated_result



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 组装审批流出参
def _build_flow_item(flow: ApprovalFlow, expense_count: int, rule_count: int) -> dict:
    """
    把审批流实体组装成管理端出参（列表、详情、新建、修改四处共用）
        额外带上"被多少单据使用、被多少规则引用"，这两个数是删除/停用的依据：
            - 被单据使用 → 不能删除；
            - 被规则引用 → 不能删除、也不能停用。
        :param flow: 审批流实体
        :param expense_count: 绑定该流程的单据数
        :param rule_count: 引用该流程的规则数
        :return: 可直接作为 AdminFlowItem 出参的字典
    """
    return {
        "id": flow.id,                                  # 审批流ID
        "name": flow.name,                              # 流程名称
        "description": flow.description,                # 流程说明
        "levels": flow.levels or [],                    # 各级审批配置
        "is_active": flow.is_active,                    # 是否启用
        "is_default": flow.is_default,                  # 是否默认流程
        "expense_count": expense_count,                 # 绑定单据数
        "rule_count": rule_count,                       # 引用规则数
        "created_at": flow.created_at,                  # 创建时间
    }



# 批量统计流程引用情况
def _load_reference_counts(db: Session, flows: list[ApprovalFlow]) -> tuple[dict, dict]:
    """
    批量统计一批流程被单据绑定、被规则引用的次数
        为什么批量：列表页可能展示多个流程，逐个查会变成 N+1。
        :param db: 数据库会话
        :param flows: 流程列表
        :return: (单据数字典, 规则数字典)，形如 ({1: 3}, {1: 2})
    """
    flow_ids = [flow.id for flow in flows]  # 流程ID列表
    if not flow_ids:
        return {}, {}

    # 1.单据引用数：一次分组查询
    expense_rows = db.query(Expense.flow_id, func.count(Expense.id)).filter(  # 单据引用统计
        Expense.flow_id.in_(flow_ids)
    ).group_by(Expense.flow_id).all()
    expense_counts = {row[0]: row[1] for row in expense_rows}  # 单据数映射

    # 2.规则引用数：规则数量很少，取出 config 在内存里比对
    rule_counts = {}  # 规则数映射
    for rule in db.query(Rule).all():
        flow_ref = (rule.config or {}).get("flow_id")  # 规则引用的流程ID
        if flow_ref is not None and int(flow_ref) in flow_ids:
            rule_counts[int(flow_ref)] = rule_counts.get(int(flow_ref), 0) + 1  # 累加引用次数

    return expense_counts, rule_counts



# 分页查询审批流列表的接口(仅admin可用)
@router.get(
    "",                                         # 接口路径；拼上总前缀后完整为 /api/v1/admin/approval-flow
    response_model=AdminFlowListResponse,       # 返回值按管理端分页响应模型格式化
    tags=["管理端"],                             # 在 /api/docs 文档里归到"管理端"分组显示
    summary="分页查询审批流列表(仅admin可用)",       # 接口标题
)
def list_flows(
    page_params: PageParams = Depends(),                                      # 分页参数
    is_active: Optional[bool] = Query(None, description="筛选:启用状态(true启用/false停用/不传则全部)"),  # 启用状态
    keyword: Optional[str] = Query(None, max_length=50, description="筛选:关键字,匹配流程名称/说明"),  # 搜索关键字
    db: Session = Depends(get_db),                                            # 注入数据库会话
    current_user: User = Depends(get_current_user),                           # 当前登录人（验签）
):
    """
    分页查询审批流列表（管理端）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
    排序:
        - 默认流程排最前，其余按ID升序：管理员最关心的就是"兜底流程是哪一条"
        :param page_params: 分页参数
        :param is_active: 可选，按启用状态筛选
        :param keyword: 可选，模糊搜索流程名称或说明
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: AdminFlowListResponse 实例
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看审批流列表")

    # 2.构建查询
    query = db.query(ApprovalFlow)  # 流程查询

    # 3.追加筛选条件
    if is_active is not None:
        query = query.filter(ApprovalFlow.is_active == is_active)  # 状态筛选
    if keyword:
        kw = keyword.strip()  # 去空格关键字
        if kw:
            like = f"%{kw}%"  # 模糊匹配串
            query = query.filter(  # 名称或说明匹配
                ApprovalFlow.name.like(like) | ApprovalFlow.description.like(like)
            )

    # 4.排序 + 分页
    query = query.order_by(ApprovalFlow.is_default.desc(), ApprovalFlow.id.asc())  # 排序规则
    flows, total = paginate_query(  # 流程分页
        query=query,                            # 已加筛选与排序的查询对象
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size,        # 每页条数
    )

    # 5.批量统计引用数并组装
    expense_counts, rule_counts = _load_reference_counts(db, flows)  # 引用数统计
    result_items = [  # 流程出参列表
        _build_flow_item(flow, expense_counts.get(flow.id, 0), rule_counts.get(flow.id, 0))
        for flow in flows
    ]

    # 6.组装分页结果
    return assemble_paginated_result(
        items=result_items,                             # 当前页流程出参
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=AdminFlowListResponse,           # 响应模型
    )



# 查询审批流详情的接口(仅admin可用)
@router.get(
    "/{flow_id}",                           # 接口路径；拼上总前缀后完整为 /api/v1/admin/approval-flow/{flow_id}
    response_model=AdminFlowItem,           # 单条流程出参
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="查询审批流详情(仅admin可用)",       # 接口标题
)
def get_flow_detail(
    flow_id: int,                                                           # 路径参数，审批流ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    查询单条审批流详情（编辑页回填）
        :param flow_id: 路径参数，审批流ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 审批流详情（含层级、引用数）
        :raises HTTPException: 403 无权限；404 流程不存在
    """
    # 1.权限校验：仅 admin
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看审批流详情")

    # 2.按ID查流程，不存在返回 404
    flow = db.query(ApprovalFlow).filter(ApprovalFlow.id == flow_id).first()  # 目标流程
    if flow is None:
        raise HTTPException(status_code=404, detail="审批流不存在")

    # 3.统计被单据/规则引用的次数（前端据此提示能否删除/停用）
    expense_count, rule_count = count_flow_references(db, flow.id)  # 引用数
    # 4.组装出参（含层级、引用数）
    return _build_flow_item(flow, expense_count, rule_count)



# 新建审批流的接口(仅admin可用)
@router.post(
    "",                             # 接口路径；拼上总前缀后完整为 /api/v1/admin/approval-flow
    response_model=AdminFlowItem,   # 返回新建的流程
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="新建审批流(仅admin可用)",  # 接口标题
)
def create_new_flow(
    payload: AdminFlowCreate,                                               # 入参：名称/说明/层级/启用/默认
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    新建一套审批流
    校验规则（详见 services.flow_service）:
        - 名称不能为空且全局唯一
        - levels 至少一级，level 必须从 1 连续递增，role 只能是 manager/finance_other/finance/boss
          （admin 没有审批权限，出现在流程里会让单据永久卡住，会被拒绝）
        - 设为默认流程时会自动清掉其他流程的默认标记，保证"全表只有一个默认流"
        :param payload: 新建入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 新建的审批流
        :raises HTTPException: 403 无权限；409 名称重复；400 层级不合法
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可新建审批流")

    # 2.调用服务层创建
    flow = create_flow(  # 新建流程
        db,                                             # 数据库会话
        name=payload.name,                              # 流程名称（全局唯一）
        description=payload.description,                # 流程说明
        levels=[level.model_dump() for level in payload.levels],   # 层级配置（转成纯字典）
        is_active=payload.is_active,                    # 是否启用
        is_default=payload.is_default,                  # 是否设为默认流程
    )

    # 3.返回（新流程必然没有引用）
    return _build_flow_item(flow, 0, 0)



# 修改审批流的接口(全量,仅admin可用)
@router.put(
    "/{flow_id}",                   # 接口路径；拼上总前缀后完整为 /api/v1/admin/approval-flow/{flow_id}
    response_model=AdminFlowItem,   # 返回修改后的流程
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="修改审批流(全量,仅admin可用)",  # 接口标题
)
def update_existing_flow(
    flow_id: int,                                                           # 路径参数，审批流ID
    payload: AdminFlowUpdate,                                               # 全量入参
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    修改指定审批流（PUT 全量：名称/说明/层级/启停/默认标记一次发全）
    停用与删除的保护:
        - 默认流程不能停用，也不能直接取消默认（请把另一条流程设为默认）
        - 被启用中的规则引用的流程不能停用，否则提交时会报"规则命中的审批流不存在或已停用"
        - 被单据使用的流程允许停用（历史单据不受影响），但不能删除
        :param flow_id: 路径参数，审批流ID
        :param payload: 全量修改入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 修改后的审批流（含引用数）
        :raises HTTPException: 403 无权限；404 流程不存在；409 名称重复；400 层级/停用规则不通过
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可修改审批流")

    # 2.查流程
    flow = db.query(ApprovalFlow).filter(ApprovalFlow.id == flow_id).first()  # 目标流程
    if flow is None:
        raise HTTPException(status_code=404, detail="审批流不存在")

    # 3.调用服务层修改
    flow = update_flow(  # 修改后的流程
        db,                                             # 数据库会话
        flow,                                           # 待修改的流程实体
        name=payload.name,                              # 流程名称
        description=payload.description,                # 流程说明
        levels=[level.model_dump() for level in payload.levels],   # 层级配置（转成纯字典）
        is_active=payload.is_active,                    # 是否启用
        is_default=payload.is_default,                  # 是否设为默认流程
    )

    # 4.返回（引用数重新统计，停用/改层级后前端需要刷新）
    expense_count, rule_count = count_flow_references(db, flow.id)  # 引用数
    return _build_flow_item(flow, expense_count, rule_count)



# 删除审批流的接口(仅admin可用)
@router.delete(
    "/{flow_id}",                   # 接口路径；拼上总前缀后完整为 /api/v1/admin/approval-flow/{flow_id}
    tags=["管理端"],                 # 在 /api/docs 文档里归到"管理端"分组显示
    summary="删除审批流(仅admin可用)",  # 接口标题
)
def delete_existing_flow(
    flow_id: int,                                                           # 路径参数，审批流ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    删除指定审批流（仅允许删除"完全没被使用"的流程）
    保护规则:
        - 被任何报销单绑定 → 409，建议改为停用
        - 被任何规则引用（含停用规则）→ 409，请先调整规则
        - 默认流程 → 409，请先把另一条流程设为默认
        :param flow_id: 路径参数，审批流ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 删除结果提示
        :raises HTTPException: 403 无权限；404 流程不存在；409 仍被引用
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可删除审批流")

    # 2.查流程
    flow = db.query(ApprovalFlow).filter(ApprovalFlow.id == flow_id).first()  # 目标流程
    if flow is None:
        raise HTTPException(status_code=404, detail="审批流不存在")

    # 3.删除（服务层做引用校验）
    delete_flow(db, flow)
    return {"message": "审批流已删除", "id": flow_id}
