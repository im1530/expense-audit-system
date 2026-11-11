"""
AI 预审路由（挂载前缀 /api/v1/agent）
    提供三个接口：
        - POST /review/{expense_id}：触发预审，pending 时投递后台任务，返回 202；
        - GET  /review/{expense_id}：查询最新一次预审结果；
        - GET  /review/{expense_id}/trace：查询工作流轨迹（节点耗时/token、决策、引用、凭证结论，只读）。
    权限：
        - 触发：单据提交人本人，或具备审批资格的人（部门经理/财务/老板）；
        - 查看：提交人本人、有 view_all 的角色、有审批资格的人；
        - admin 不参与审批，只能查看，不能触发。
    说明：
        - 后台任务只负责执行，接口不在请求线程里调 LLM；
        - 真正的权限/状态校验在 service 层还会再做一次，避免并发窗口。
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.dependencies import get_db
from app.models import AIReviewLog, AIReviewStatus, Department, Expense, User
from app.schemas.ai_review import AIFinding, AIReviewResponse, PolicyCitation
from app.schemas.workflow_trace import WorkflowTraceResponse
from app.services.ai_review_service import (
    ReviewError,
    create_review,
    get_latest_review,
)
from app.services.review_queue import dispatch_review
from app.services.workflow_trace_service import build_workflow_trace



router = APIRouter()

logger = logging.getLogger(__name__)



# 取角色字符串（枚举安全）
def _role(user: User) -> str:
    """
    把用户角色统一成字符串
        :param user: 用户
        :return: admin/boss/finance/manager/employee
    """
    return user.role.value if hasattr(user.role, "value") else str(user.role)



# 判断是否是申请人部门的经理
def _is_department_manager(db: Session, user: User, expense: Expense) -> bool:
    """
    当前用户是否是申请人所属部门的负责人
        :param db: 数据库会话
        :param user: 当前用户
        :param expense: 报销单
        :return: 是则 True
    """
    if expense.user is None or expense.user.department_id is None:
        return False
    if _role(user) != "manager":
        return False
    department = (
        db.query(Department)
        .filter(Department.id == expense.user.department_id)
        .first()
    )
    return department is not None and department.manager_id == user.id



# 是否有审批资格（用于触发和查看）
def _can_approve_expense(db: Session, user: User, expense: Expense) -> bool:
    """
    判断用户是否有资格审批这张单（不校验当前层级，只校验角色范围）
        :param db: 数据库会话
        :param user: 当前用户
        :param expense: 报销单
        :return: 有资格则 True
    """
    if not user.has_permission("approve"):
        return False
    role = _role(user)
    if role in ("finance", "boss"):
        return True
    if role == "manager":
        return _is_department_manager(db, user, expense)
    return False



# 触发权限：本人或审批人
def _can_trigger(db: Session, user: User, expense: Expense) -> bool:
    """
    判断用户能否触发预审
        :param db: 数据库会话
        :param user: 当前用户
        :param expense: 报销单
        :return: 允许则 True
    """
    if expense.user_id == user.id:
        return True
    return _can_approve_expense(db, user, expense)



# 查看权限：本人 / 全局查看 / 审批人
def _can_read(db: Session, user: User, expense: Expense) -> bool:
    """
    判断用户能否查看预审结果
        :param db: 数据库会话
        :param user: 当前用户
        :param expense: 报销单
        :return: 允许则 True
    """
    if expense.user_id == user.id:
        return True
    if user.has_permission("view_all"):
        return True
    return _can_approve_expense(db, user, expense)



# 业务错误 → HTTP 状态码
def _http_status(code: str) -> int:
    """
    把 ReviewError.code 映射成 HTTP 状态码
        :param code: NOT_FOUND / CONFLICT / FORBIDDEN / 其他
        :return: HTTP 状态码
    """
    return {"NOT_FOUND": 404, "CONFLICT": 409, "FORBIDDEN": 403}.get(code, 400)



# ORM 日志 → API 响应
def _to_response(log: AIReviewLog, *, scheduled: bool = False) -> AIReviewResponse:
    """
    把预审日志转成响应模型（不暴露 raw_response）
        :param log: 预审日志
        :param scheduled: 本次是否投递了后台任务
        :return: AIReviewResponse
    """
    findings = [AIFinding.model_validate(item) for item in (log.findings or [])]
    citations = [PolicyCitation.model_validate(item) for item in (log.policy_citations or [])]
    return AIReviewResponse(
        review_id=log.id,
        expense_id=log.expense_id,
        status=log.status,
        risk_score=log.risk_score,
        risk_level=log.risk_level,
        summary=log.summary,
        findings=findings,
        policy_citations=citations,
        is_fallback=log.is_fallback,
        model=log.model or "",
        prompt_version=log.prompt_version or "",
        policy_version=log.policy_version or "",
        decision=log.decision or "",
        decision_reason=log.decision_reason or "",
        workflow_run_id=log.workflow_run_id,
        latency_ms=log.latency_ms,
        prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens,
        cost=log.cost,
        error_message=log.error_message,
        scheduled=scheduled,
        created_at=log.created_at,
        updated_at=log.updated_at,
    )



# 触发 AI 预审
@router.post(
    "/review/{expense_id}",                     # 路径：/api/v1/agent/review/{expense_id}
    response_model=AIReviewResponse,            # 响应模型
    status_code=202,                            # 默认 202；已有结果时改为 200
    tags=["AI预审"],                             # 文档分组
    summary="触发报销单 AI 预审",                  # 接口标题
)
def trigger_ai_review(
    expense_id: int,                                                # 路径参数：报销单ID
    response: Response,                                             # 用于动态改状态码
    background_tasks: BackgroundTasks,                              # 后台任务投递器
    db: Session = Depends(get_db),                                  # 数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人
    force: bool = False,                                            # 是否忽略缓存强制重跑
):
    """
    触发一次 AI 预审
        行为：
            - 单据不存在 → 404；
            - 无触发权限 → 403；
            - 非待审批/已有结果/缓存命中 → 409 或 200；
            - 新建 pending → 投递后台任务，返回 202。
        :param expense_id: 报销单ID
        :param response: FastAPI 响应对象（动态状态码）
        :param background_tasks: 后台任务
        :param db: 数据库会话
        :param current_user: 当前用户
        :param force: 是否忽略缓存强制重跑
        :return: AIReviewResponse
        :raises HTTPException: 403/404/409/400
    """
    # 1.查单据（不存在直接 404）
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在")

    # 2.触发权限
    if not _can_trigger(db, current_user, expense):
        raise HTTPException(status_code=403, detail="无权触发该报销单的 AI 预审")

    # 3.创建任务（service 会再做状态/幂等/缓存校验）
    try:
        log = create_review(
            db,
            expense_id=expense_id,
            triggered_by=current_user.id,
            trigger_source="manual",
            force=force,
        )
    except ReviewError as exc:
        raise HTTPException(status_code=_http_status(exc.code), detail=exc.message) from exc

    # 4.只有 pending 才投递；优先 Celery，队列不可用时回退 BackgroundTasks
    scheduled = False
    if log.status == AIReviewStatus.PENDING:
        scheduled = dispatch_review(log.id, background_tasks)
        response.status_code = 202
    else:
        response.status_code = 200

    logger.info(
        "触发 AI 预审 expense_id=%s review_id=%s status=%s scheduled=%s user_id=%s",
        expense_id, log.id, log.status, scheduled, current_user.id,
    )
    return _to_response(log, scheduled=scheduled)



# 查询最新预审结果
@router.get(
    "/review/{expense_id}",                     # 路径：/api/v1/agent/review/{expense_id}
    response_model=AIReviewResponse,            # 响应模型
    tags=["AI预审"],                             # 文档分组
    summary="查询报销单最新 AI 预审结果",           # 接口标题
)
def get_ai_review(
    expense_id: int,                                                # 路径参数：报销单ID
    db: Session = Depends(get_db),                                  # 数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人
):
    """
    查询某张单据最新一次预审结果
        :param expense_id: 报销单ID
        :param db: 数据库会话
        :param current_user: 当前用户
        :return: AIReviewResponse
        :raises HTTPException: 404 单据/记录不存在；403 无权限
    """
    # 1.查单据
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在")

    # 2.查看权限
    if not _can_read(db, current_user, expense):
        raise HTTPException(status_code=403, detail="无权查看该报销单的 AI 预审结果")

    # 3.取最新一次记录
    log = get_latest_review(db, expense_id)
    if log is None:
        raise HTTPException(status_code=404, detail="该报销单尚无 AI 预审记录")

    return _to_response(log)



# 查询 AI 预审工作流轨迹（只读，供联调/排障）
@router.get(
    "/review/{expense_id}/trace",               # 路径：/api/v1/agent/review/{expense_id}/trace
    response_model=WorkflowTraceResponse,       # 响应模型
    tags=["AI预审"],                             # 文档分组
    summary="查询报销单 AI 预审工作流轨迹",         # 接口标题
)
def get_review_trace(
    expense_id: int,                                                # 路径参数：报销单ID
    db: Session = Depends(get_db),                                  # 数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人
):
    """
    查询某张单据最近一次 AI 预审的工作流轨迹
        - 返回：run 状态、决策、风险结论、节点耗时/token、制度引用、凭证识别结论；
        - 只读：不触发预审、不修改任何状态；
        - 权限与查询预审结果一致：本人 / view_all / 有审批资格的人。
        :param expense_id: 报销单ID
        :param db: 数据库会话
        :param current_user: 当前用户
        :return: WorkflowTraceResponse
        :raises HTTPException: 404 单据或轨迹不存在；403 无权限
    """
    # 1.查单据（不存在直接 404）
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在")

    # 2.查看权限：与预审结果接口保持一致
    if not _can_read(db, current_user, expense):
        raise HTTPException(status_code=403, detail="无权查看该报销单的 AI 预审轨迹")

    # 3.组装轨迹；没有 run 说明还没跑过工作流
    trace = build_workflow_trace(db, expense_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="该报销单尚无 AI 预审工作流记录")

    return trace
