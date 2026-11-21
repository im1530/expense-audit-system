"""
审批模块路由（审批执行 + 审批工作台）
    覆盖审批全流程的接口，按能力分为三类：
        - 审批执行：POST /approvals/{expense_id}，对单张报销单做通过/驳回；
          内置自批拦截、当前层级角色校验、通过后自动推进层级或终审通过；
        - 审批工作台：GET /approvals/pending（我的待办）、GET /approvals/processed（我的已办），
          支持通用业务筛选 + 指定提交人/部门，前者因需按审批流层级做权限判断而采用"SQL 筛选 + 内存过滤 + 内存分页"；
        - 基础字典：GET /approvals/statuses，返回审批记录状态枚举，供前端筛选与标签展示。
    一致性要求：审批执行与待办列表的"资格判定规则"必须完全一致（可见即可批），
    因此两处对 manager / finance_other / finance / boss 四类角色的判断逻辑是刻意重复实现的，
    后续若修改审批流层级语义，两处需同步修改。
"""



from datetime import datetime, date
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.dependencies import get_db                          # 数据库会话
from app.api.deps import get_current_user                    # 当前登录用户
from app.schemas.approval import ApprovalAction, ApprovalResponse
from app.models import User, Expense, Approval, Department, ApprovalFlow
from app.models.expense import ExpenseStatus
from app.models.approval import ApprovalStatus
from app.schemas.common import PageParams, BusinessFilter
from app.schemas.expense import PendingApprovalListResponse, DictOption
from app.services.expense_service import apply_business_filters
from app.services.rule_risk_service import load_risk_map, attach_risk_marks
from app.utils.pagination import paginate_list, assemble_paginated_result, paginate_query
from typing import Optional, List



# 创建路由器对象
router = APIRouter()  # 路由对象

# 日志器：审批动作是流程里的关键节点，必须记录审批人、层级与结果
logger = logging.getLogger(__name__)  # 日志器



# 审批人审批报销单的接口
@router.post(
    "/{expense_id}",                    # 接口路径；拼上总前缀后完整为 /api/v1/approval/{expense_id}
    response_model=ApprovalResponse,         # 返回值按 ApprovalResponse 格式化
    tags=["审批"],                            # 在 /api/docs 文档里归到"审批"分组显示
    summary="审批报销单",                      # 接口标题
)
def approval_action(
    expense_id: int,                                 # 新增:路径参数,从URL中获取报销单ID
    action: ApprovalAction,                          # 接收前端请求体，并按 ApprovalAction 自动校验字段
    db: Session = Depends(get_db),                   # 依赖注入：自动拿到一个数据库会话
    current_user: User = Depends(get_current_user)   # 依赖注入：自动拿到当前登录用户
):
    """
    审批执行：对指定报销单提交"通过"或"驳回"，是审批流程的核心写入口
    权限规则:
        - 必须有 approve 权限（manager / finance / boss），否则 403
        - 不能审批本人提交的单据，从入口处拦截自批风险
        - 按单据绑定的审批流 levels 定位当前层级要求的角色，再校验当前用户是否具备本级资格：
            · manager        ：必须是申请人所属部门的经理（department.manager_id）
            · finance_other  ：任一财务均可，但排除申请人本人
            · finance / boss ：角色字符串直接匹配
        该规则与待办列表逐条一致，保证"可见即可批"。
    业务处理:
        1. 校验单据存在、状态为 pending、已绑定有效审批流；
        2. 生成一条审批记录（含审批人、层级、结果、意见）；
        3. 驳回：单据置为 rejected 并记录 approved_at，流程终止；
        4. 通过：存在下一级则 current_level + 1 并保持 pending，否则置为 approved 终审通过。
    异常: 403 无权限/自批/层级不符；404 单据不存在；409 状态不符；400 审批流配置缺失或无效；
          422 审批结论不是 approved/rejected（由 ApprovalAction 的校验器拦截，不会进入业务逻辑）
        :param expense_id: 路径参数，要审批的报销单ID
        :param action: 审批动作入参（status 仅 approved 或 rejected，comment 选填）
        :param db: 数据库会话
        :param current_user: 当前登录审批人
        :return: 新建的审批记录，经 response_model=ApprovalResponse 过滤字段
    """
    # 1.功能权限校验:必须有 approve 权限(manager/finance/boss)
    if not current_user.has_permission("approve"):
        raise HTTPException(status_code=403, detail="无审批权限")


    # 2.查报销单,不存在则404；加行锁，避免两个审批人并发推进同一张单
    expense = (
        db.query(Expense)
        .filter(Expense.id == expense_id)
        .with_for_update()
        .first()
    )
    if not expense:
        raise HTTPException(status_code=404, detail="报销单不存在")


    # 3.状态校验:只有待审批状态的报销单才能审批
    if expense.status not in (ExpenseStatus.PENDING,):
        raise HTTPException(status_code=409, detail="仅待审批单据可以进行审批")


    # 4.安全性校验:不能审批自己提交的报销单,提前拦截
    if expense.user_id == current_user.id:
        raise HTTPException(status_code=403, detail="不能审批自己的报销单")


    # 5.加载审批流,取出级别配置列表 levels
    if not expense.flow_id:
        raise HTTPException(status_code=400, detail="未配置审批流")
    flow = db.query(ApprovalFlow).filter(ApprovalFlow.id == expense.flow_id).first()    # 加载审批流
    if not flow or not flow.is_active or not flow.levels:
        raise HTTPException(status_code=400, detail="审批流已停用或配置无效")
    levels = flow.levels  #     取出级别配置列表: [{"level":1,"role":"manager"},...]


    # 6.按当前层级取出本级要求的角色
    # next(..., None):在列表里按条件找一项，找不到给默认值 None
    # 用生成器遍历 levels,找到 level 编号 == 单据当前层级(current_level)的那一项;找不到则返回 None
    current = next(  # 当前层级配置
        # levels 是审批流里的列表,形如 [{"level":1,"role":"manager"},{"level":2,"role":"finance"},{"level":3,"role":"boss"}]
        (lv for lv in levels if lv.get("level") == expense.current_level),  # 当前层级配置项
        None  # 找不到时的默认值
    )   # 这里 current 取出来是一个字典(列表里的一项)
    # 没找到对应层级配置 -> 400(流配置和当前层级对不上)
    if current is None:
        raise HTTPException(status_code=400, detail="当前审批层级配置缺失")
    # 从这一项里取出本级要求的审批人角色
    required_role = current.get("role")  # 本级要求角色
    # 取当前登录审批人的角色(枚举取.value,否则直接用)
    approver_role = current_user.role.value if hasattr(current_user.role, "value") else current_user.role  # 当前用户角色


    # 7.本级资格校验:按本级要求的审批人角色走不同的校验逻辑
    if required_role == "manager":
        # 7.1 本级要求 manager:必须是申请人所属部门的经理
        # 先查出申请人信息
        applicant = db.query(User).filter(User.id == expense.user_id).first()  # 申请人实体
        # 申请人没有所属部门,则无法定位其部门经理,直接拦截
        if not applicant or not applicant.department_id:
            raise HTTPException(status_code=403, detail="申请人未归属部门,无法匹配审批人")
        # 有所属部门,则查出申请人所在部门
        department = db.query(Department).filter(Department.id == applicant.department_id).first()  # 申请人部门
        # 校验:部门存在 + 当前用户是该部门经理 + 当前用户角色确实是 manager
        if not department or current_user.id != department.manager_id or approver_role != "manager":
            raise HTTPException(status_code=403, detail="无权审批,应由该部门经理审批")

    elif required_role == "finance_other":
        # 7.2 本级要求其他财务:通 finance 角色且不能是申请人本人
        # 登录角色不是finance,拦截
        if approver_role != "finance":
            raise HTTPException(status_code=403, detail="无权审批,应由其他财务人员审批")
        # 是申请人本人,拦截自批
        if expense.user_id == current_user.id:
            raise HTTPException(status_code=403, detail="无权审批,不能审批自己的报销单")

    else:
        # 7.3 其他要求的审批人角色(finance/boss):直接按角色字符串匹配
        if approver_role != required_role:
            raise HTTPException(status_code=403, detail=f"无权审批,本级应由 {required_role} 审批")


    # 8.定位本级待办：提交/上一级通过时创建，审批动作直接更新它，不再新增一条结果记录
    approval = (
        db.query(Approval)
        .filter(
            Approval.expense_id == expense_id,
            Approval.level == expense.current_level,
            Approval.status == ApprovalStatus.PENDING,
        )
        .order_by(Approval.id.desc())
        .first()
    )
    if approval is None:
        # 兼容历史数据：旧流程可能没有为本级建待办，补一条再更新，避免 500
        approval = Approval(
            expense_id=expense_id,
            approver_id=None,
            flow_id=expense.flow_id,
            status=ApprovalStatus.PENDING,
            comment="系统补建待办",
            level=expense.current_level,
        )
        db.add(approval)

    now = datetime.now()
    approval.approver_id = current_user.id          # 回填实际审批人
    approval.status = action.status                 # pending → approved / rejected
    approval.comment = action.comment               # 审批意见
    approval.acted_at = now                         # 处理时间

    # 9.处理审批结果
    if action.status == ApprovalStatus.REJECTED:
        # 9.1 拒绝:直接终止,报销单状态为 rejected,并记录审批时间
        expense.status = ExpenseStatus.REJECTED  # 状态：已驳回
        expense.approved_at = now  # 记录审批通过时间
    else:
        # 9.2 通过:判断是否还有下一级审批 --- any(...)：只要有一个满足就返回 True，全不满足返回 False
        next_level = expense.current_level + 1  # 计算下一级别(当前级别 + 1)
        has_next = any(lv.get("level") == next_level for lv in levels)  # 是否还有下一级
        if has_next:
            # 还有下一级:推进层级,为下一级创建新的待办,单据保持 pending
            expense.current_level = next_level  # 推进层级
            expense.status = ExpenseStatus.PENDING  # 状态：待下一级
            db.add(Approval(
                expense_id=expense_id,
                approver_id=None,
                flow_id=expense.flow_id,
                status=ApprovalStatus.PENDING,
                comment="等待本级审批",
                level=next_level,
            ))
        else:
            # 没有下一级:报销单状态为 approved,并记录审批时间
            expense.status = ExpenseStatus.APPROVED  # 状态：终审通过
            expense.approved_at = now  # 记录审批通过时间


    # 10.写入数据库的三步
    db.commit()            # 提交事务,真正落库
    db.refresh(approval)   # 回读数据库,把 id、默认值等刷新到 approval 对象上


    # 11.返回:由 response_model=ApprovalResponse 过滤字段
    logger.info(
        "审批操作 expense_id=%s level=%s approver_id=%s result=%s 单据状态=%s",  # 日志格式串
        expense.id,                                                     # 报销单ID
        approval.level,                                                 # 审批层级
        current_user.id,                                                # 审批人ID
        action.status.value if hasattr(action.status, "value") else action.status,     # 审批结果
        expense.status.value if hasattr(expense.status, "value") else expense.status,  # 单据状态
    )
    return approval



# 分页查看审批待办列表的接口
@router.get(
    "/pending",                               # 接口路径；拼上总前缀后完整为 /api/v1/approval/pending
    response_model=PendingApprovalListResponse,    # 返回值按 PendingApprovalListResponse 格式化
    tags=["审批"],                                  # 在 /api/docs 文档里归到"审批"分组显示
    summary="获取我的待审批报销单列表"                 # 接口标题
)
def get_pending_approval_list(
    page_params: PageParams = Depends(),              # 分页参数
    common_filter: BusinessFilter = Depends(),        # 通用业务筛选参数
    user_id: Optional[int] = Query(None, ge=1, description="筛选:指定员工提交的报销单"),   # 按提交人筛选
    department_id: Optional[int] = Query(None, ge=1, description="筛选:指定部门的报销单"),  # 按部门筛选
    db: Session = Depends(get_db),                    # 依赖注入：自动拿到一个数据库会话
    current_user: User = Depends(get_current_user),   # 依赖注入：自动拿到当前登录用户
):
    """
    获取当前登录审批人的待审批报销单列表（审批工作台首页）
    定位: 展示"此刻轮到我来处理"的审批任务，是审批人最主要的入口。
    权限规则:
        - 必须有 approve 权限，否则 403；且全局排除本人提交的单据（与审批接口的禁自批规则对齐）
        - manager       ：仅本部门员工提交、且当前层级要求 manager 的单据
        - finance       ：当前层级要求 finance 或 finance_other 的单据（后者排除本人提交）
        - boss          ：当前层级要求 boss 的单据
    两段式处理（为什么不能全部下推到 SQL）:
        - 第一段：SQL 层完成状态过滤（pending）、自单排除、通用业务筛选、指定人/部门筛选与时间排序；
        - 第二段：因"当前层级要求什么角色"存放在 approval_flows.levels 这个 JSON 里，
          SQL 无法直接表达，只能逐条解析流程、判断资格后再过滤；
        - 因过滤发生在内存中，分页也必须在过滤之后做（paginate_list），否则 total 与当前页数据会对不上；
        - 被判定的依据（审批流模板、部门负责人）会先一次性预加载成字典，循环里只做内存判断，不再逐条查库。
    返回: 标准化分页结构，列表项为精简的 PendingApprovalItem
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数（费用大类/小类、日期区间、金额区间）
        :param user_id: 可选，只看指定提交人的单据
        :param department_id: 可选，只看指定部门的单据
        :param db: 数据库会话
        :param current_user: 当前登录审批人
        :return: PendingApprovalListResponse 实例
        :raises HTTPException: 403 当前用户无审批权限
    """
    # 1.功能权限校验:只有拥有审批权限的角色才能获取待办列表
    if not current_user.has_permission("approve"):
        raise HTTPException(status_code=403, detail="无审批权限,无法获取待审批列表")


    # 2.构建基础查询:仅待审批状态单据;关联用户表带出部门ID,为后续部门校验、部门筛选打底
    query = db.query(  # 待办基础查询
        Expense, User.department_id  # 选择报销单表和用户表的部门ID字段
    ).join(User, Expense.user_id == User.id  # 关联用户表,带出部门ID字段
           ).filter(
        Expense.status == ExpenseStatus.PENDING,  # 过滤:仅待审批状态单据
        Expense.user_id != current_user.id  # 全局自单排除:过滤掉当前用户自己提交的报销单
        # 与审批执行接口禁止自批规则对齐,实现可见即可批
    )


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_business_filters(query, common_filter)  # 追加通用筛选

    # 筛选:指定提交人
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选

    # 筛选:指定部门
    if department_id is not None:
        query = query.filter(User.department_id == department_id)  # 按部门筛选


    # 4.排序:默认按照提交时间倒序,最新提交的排在前面
    query = query.order_by(Expense.submitted_at.desc())  # 按提交时间倒序


    # 5.执行查询,拿到初步业务筛选后的数据集(行结构:(报销单, 提交人部门ID))
    filtered_rows = query.all()  # 候选单据列表


    # 5.1 预加载审批流:一次把本次涉及的流程全部查出来做成字典,避免循环里逐条查库(N+1)
    # set 推导式先去重:同一套流程可能被多张单据共用,没必要重复查
    flow_ids = {expense.flow_id for expense, _department_id in filtered_rows if expense.flow_id}  # 涉及的流程ID
    flow_map = {}  # 流程ID → 流程实体
    if flow_ids:
        flow_map = {  # 流程ID → 流程实体
            flow.id: flow  # 流程ID
            for flow in db.query(ApprovalFlow).filter(ApprovalFlow.id.in_(flow_ids)).all()
        }

    # 5.2 预加载部门:只有 manager 层级才需要判断"申请人所在部门的负责人是不是我",同样一次查完
    # 提交人部门ID在基础查询里已经带出,这里不必再查用户表
    department_ids = {department_id for _expense, department_id in filtered_rows if department_id}  # 涉及的部门ID
    department_map = {}  # 部门ID → 部门实体
    if department_ids:
        department_map = {  # 部门ID → 部门实体
            department.id: department  # 部门ID
            for department in db.query(Department).filter(Department.id.in_(department_ids)).all()
        }


    # 6.按权限规则过滤待审批报销单
    # 取出当前登录审批人的角色
    approver_role = current_user.role.value if hasattr(current_user.role, "value") else current_user.role  # 当前用户角色
    # 只保留当前用户有权审批的报销单
    my_pending_approval_list = []  # 我的待办列表
    for expense, applicant_department_id in filtered_rows:
        # 6.1 从预加载的字典里取该报销单绑定的审批流模板,取不到或没配层级则跳过该报销单
        flow = flow_map.get(expense.flow_id)  # 单据审批流
        if not flow or not flow.is_active or not flow.levels:
            continue

        # 6.2 定位当前审批层级对应的配置项,若不存在则跳过该报销单
        current_level_config = next(  # 当前层级配置
            (level for level in flow.levels if level.get("level") == expense.current_level),  # 当前层级配置项
            None  # 找不到时的默认值
        )
        if not current_level_config:
            continue

        # 6.3 取出本级要求的审批角色
        required_role = current_level_config.get("role")  # 本级要求角色

        # 6.4 分角色匹配校验,逻辑与审批接口完全一致
        # 标记当前用户是否有权审批本级
        has_approval_permission = False  # 默认无审批资格

        if required_role == "manager":
            # manager级:仅申请人所属部门的经理可见(部门ID已由基础查询带出,直接查预加载的字典)
            department = department_map.get(applicant_department_id)  # 申请部门
            if department and department.manager_id == current_user.id and approver_role == "manager":
                has_approval_permission = True  # 本级有审批资格

        elif required_role == "finance_other":
            # finance_other级:所有财务可见,申请人本人除外
            if approver_role == "finance" and expense.user_id != current_user.id:
                has_approval_permission = True  # 本级有审批资格

        elif required_role == "finance":
            # finance级:所有财务可见
            if approver_role == "finance":
                has_approval_permission = True  # 本级有审批资格

        elif required_role == "boss":
            # boss级:仅公司老板可见
            if approver_role == "boss":
                has_approval_permission = True  # 本级有审批资格

        # 6.5 若当前用户有权审批本级,则加入我的待办列表
        if has_approval_permission:
            my_pending_approval_list.append(expense)


    # 7.分页处理
    items, total = paginate_list(  # 内存分页
        data_list=my_pending_approval_list,     # 已过滤完成的数据列表
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size         # 每页条数
    )


    # 7.1 批量加载本页单据的规则命中并挂成风险标记（一次 SQL，避免逐单查询）
    risk_map = load_risk_map(  # 加载风险标记
        db,                                     # 数据库会话
        [expense.id for expense in items],      # 本页报销单ID
    )
    attach_risk_marks(
        items,                                  # 本页报销单 ORM 对象
        risk_map,                               # {报销单ID: 风险汇总}
    )


    # 8.构造响应数据字典
    pending_expense = assemble_paginated_result(  # 待办分页响应
        items=items,                                    # 当前页的报销单列表（已分页）
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=PendingApprovalListResponse,     # 响应模型
    )


    # 9.返回:由 response_model=PendingApprovalListResponse 过滤字段
    return pending_expense



# 分页查看我已审批的报销单列表的接口
@router.get(
    "/processed",                              # 接口路径；拼上总前缀后完整为 /api/v1/approval/approved
    response_model=PendingApprovalListResponse,     # 返回值按 PendingApprovalListResponse 格式化
    tags=["审批"],                                   # 在 /api/docs 文档里归到"审批"分组显示
    summary="获取我的已审批的报销单列表"                 # 接口标题
)
def get_processed_approval_list(
    page_params: PageParams = Depends(),              # 分页参数
    common_filter: BusinessFilter = Depends(),        # 通用业务筛选参数
    status: Optional[ExpenseStatus] = Query(None, description="筛选:报销单当前状态"),              # 按单据状态筛选
    approval_result: Optional[ApprovalStatus] = Query(None, description="筛选:我的审批结果(通过/拒绝)"),  # 按我的审批结果筛选
    approval_start_date: Optional[date] = Query(None, description="筛选:审批操作日期(起始)"),        # 审批日期区间起点
    approval_end_date: Optional[date] = Query(None, description="筛选:审批操作日期(结束)"),          # 审批日期区间终点
    user_id: Optional[int] = Query(None, ge=1, description="筛选:指定员工提交的报销单"),             # 按提交人筛选
    department_id: Optional[int] = Query(None, ge=1, description="筛选:指定部门的报销单"),          # 按部门筛选
    db: Session = Depends(get_db),                    # 依赖注入：自动拿到一个数据库会话
    current_user: User = Depends(get_current_user),   # 依赖注入：自动拿到当前登录用户
):
    """
    获取当前登录用户"已处理"的报销单列表（审批工作台账）
    定位: 用于留痕追溯、工作复盘与进度跟进，展示"我批过的单现在到哪一步了"。
    权限规则:
        - 必须有 approve 权限，否则 403
        - 数据范围强制限定为"本人审批过的单据"（Approval.approver_id = 当前用户），天然隔离
    处理说明:
        - 与待办不同，这里不需要判断层级资格，因此可以完全下推 SQL，用 paginate_query 分页
        - 同一张单据可能被我多次审批（多级/重新提交），用 distinct(Expense.id) 去重，只保留一行
        - 时间区间筛选针对 approval.created_at（审批动作发生时间），不是单据创建时间
    返回: 与待办同构的分页结构（列表项复用 PendingApprovalItem）
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按单据当前状态筛选
        :param approval_result: 可选，只看我"通过"或"驳回"的记录
        :param approval_start_date: 可选，审批操作日期区间起点
        :param approval_end_date: 可选，审批操作日期区间终点
        :param user_id: 可选，只看指定提交人的单据
        :param department_id: 可选，只看指定部门的单据（按需 join 用户表）
        :param db: 数据库会话
        :param current_user: 当前登录审批人
        :return: PendingApprovalListResponse 实例
        :raises HTTPException: 403 当前用户无审批权限
    """
    # 1.权限校验:无审批权限的用户直接返回拦截
    if not current_user.has_permission("approve"):
        raise HTTPException(status_code=403, detail="无审批权限,无法获取已审批列表")


    # 2.构建基础查询:关联审批记录表与报销单表,强制过滤审批人为当前登录用户
    # 用 group_by(Expense.id) 而不是 distinct(Expense.id)：
    #   MySQL 8 下 "SELECT DISTINCT 整行 + ORDER BY approvals.created_at" 会报 3065
    #   （排序列不在 SELECT 列表中）；改为按报销单分组、按"我的最近一次审批时间"排序，
    #   既完成去重，也让排序键出现在查询里，分页 count 仍然按单据数统计。
    query = (  # 已办基础查询
        db.query(Expense)                                   # 报销单主表
        .join(Approval, Expense.id == Approval.expense_id)  # 关联审批记录
        .filter(Approval.approver_id == current_user.id)    # 只看我审批过的记录
        .group_by(Expense.id)                               # 同一单据多次审批只保留一行
    )


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_business_filters(query, common_filter)  # 追加通用筛选

    # 筛选:按报销单状态
    if status is not None:
        query = query.filter(Expense.status == status)  # 按单据状态筛选

    # 筛选:按我的审批结果
    if approval_result is not None:
        query = query.filter(Approval.status == approval_result)  # 按我的审批结果筛选

    # 筛选:按审批操作日期区间
    if approval_start_date is not None:
        query = query.filter(Approval.created_at >= approval_start_date)  # 审批时间 ≥ 起始日
    if approval_end_date is not None:
        query = query.filter(Approval.created_at <= approval_end_date)  # 审批时间 ≤ 结束日

    # 筛选:指定提交人
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选

    # 筛选:指定部门
    # 部门信息存储在用户表中，需要关联用户表才能按部门过滤报销单
    if department_id is not None:
        query = query.join(User, Expense.user_id == User.id).filter(  # 关联用户表（按部门筛选）
            User.department_id == department_id  # 按部门筛选
        )


    # 4.排序规则:按"我的最近一次审批操作时间"倒序,最近处理的单在最前
    query = query.order_by(func.max(Approval.created_at).desc())  # 按最近审批时间倒序


    # 5.分页计算:
    items, total = paginate_query(  # 分页查询
        query=query,                        # 查询对象
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
    )


    # 5.1 批量加载本页单据的规则命中并挂成风险标记（已办列表同样展示"当时提示了什么风险"）
    risk_map = load_risk_map(  # 加载风险标记
        db,                                     # 数据库会话
        [expense.id for expense in items],      # 本页报销单ID
    )
    attach_risk_marks(
        items,                                  # 本页报销单 ORM 对象
        risk_map,                               # {报销单ID: 风险汇总}
    )


    # 6.构造响应数据字典
    processed_expenses = assemble_paginated_result(  # 已办分页响应
        items=items,                                    # 当前页的报销单列表（已分页）
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=PendingApprovalListResponse,     # 响应模型
    )


    # 7.返回:由 response_model=PendingApprovalListResponse 过滤字段
    return processed_expenses



# 获取审批结果枚举列表的接口
@router.get(
    path="/statuses",                           # 接口路径；拼上总前缀后完整为 /api/v1/approval/statuses
    response_model=List[DictOption],            # 返回值按 List[DictOption] 格式化
    tags=["审批"],                               # 在 /api/docs 文档里归到"审批"分组显示
    summary="获取审批结果枚举列表",                 # 接口标题
)
def get_approval_status_list():
    """
    获取审批记录状态枚举选项（公开接口，无需登录）
    业务用途:
        - 审批记录列表页按结果筛选的筛选器数据源
        - 单据详情页审批轨迹的状态标签映射
    说明:
        - 枚举项与 ApprovalStatus 定义完全一致，按枚举定义顺序返回
        - 中文名称集中映射在 status_label_map，新增枚举值时必须同步补充，否则会触发 KeyError
        :return: 状态字典列表，元素为 {"value": 枚举值, "label": 中文名}
    """
    # 1.枚举值与中文名映射,与枚举定义语义一一对应
    status_label_map = {  # 状态中文映射
        ApprovalStatus.PENDING:"待审批",
        ApprovalStatus.APPROVED: "已通过",
        ApprovalStatus.REJECTED: "已驳回",
        ApprovalStatus.RECALLED:"已撤回",
        ApprovalStatus.CANCELLED:"已撤销",
        ApprovalStatus.OBSOLETED:"已作废"
    }

    # 2.遍历所有枚举成员,组装成标准选项格式
    status_list = [  # 状态字典列表
        {"value": status_item.value, "label": status_label_map[status_item]} for status_item in ApprovalStatus
    ]

    # 3.返回:由 response_model=List[DictOption] 统一转换并过滤字段
    return status_list
