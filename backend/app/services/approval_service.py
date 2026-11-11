"""
审批服务（跨接口复用的审批判定逻辑）
    目前提供"某人当前还有多少条待他处理的审批"这一项判断，供管理端在停用/降级账号前做拦截：
        - 与 api/v1/approval.py 的待办列表使用同一套资格规则，保证"能看到的待办"和"会被拦住的待办"完全一致；
        - 判定口径：
            · manager        ：必须是申请人所属部门的负责人（以 departments.manager_id 为准）
            · finance_other  ：任一财务，但排除申请人本人
            · finance / boss ：角色直接匹配
    维护提醒：审批流的层级语义若发生变化，本文件与 approval.py 的待办列表必须同步修改，否则会出现
    "停用接口认为没有待办、待办列表里却有一条"的不一致。
"""
from sqlalchemy.orm import Session

from app.models import User, Expense, Department, ApprovalFlow
from app.models.expense import ExpenseStatus



# 统计某用户当前待处理的审批条数
def count_pending_approvals_for_user(db: Session, user: User) -> int:
    """
    统计"当前有多少条待审批单据正等着这个人处理"
        实现方式：取出全部待审批单据 → 预加载审批流/部门/申请人部门 → 在内存里按资格规则判断。
        为什么不用 SQL 表达：当前层级要求什么角色存在 approval_flows.levels 的 JSON 里，
        SQL 无法直接判断，只能逐条解析（待办单据通常不多，预加载后成本可控）。
        :param db: 数据库会话
        :param user: 待判断的用户（通常是准备被停用/降级的账号）
        :return: 待处理条数，0 表示没有待办、可以安全停用
    """
    # 1.取所有待审批单据，并排除本人提交的（任何人都不能审批自己的单据）
    pending_expenses = db.query(Expense).filter(    # 待审批单据查询
        Expense.status == ExpenseStatus.PENDING,    # 只统计待审批状态
        Expense.user_id != user.id,                 # 排除本人提交的单据
    ).all()
    if not pending_expenses:
        return 0

    # 2.预加载审批流：一次查完，避免循环里逐条查库
    flow_ids = {expense.flow_id for expense in pending_expenses if expense.flow_id}   # 涉及的审批流ID
    flow_map = {}                                   # 审批流ID→实体
    if flow_ids:
        flow_map = {                                # 审批流ID→实体
            flow.id: flow                           # 流程ID: 流程实体
            for flow in db.query(ApprovalFlow).filter(ApprovalFlow.id.in_(flow_ids)).all()
        }

    # 3.预加载申请人的部门和部门的负责人，用于判断 manager 级待办归属
    applicant_ids = {expense.user_id for expense in pending_expenses}   # 申请人ID集合
    applicant_rows = db.query(User.id, User.department_id).filter(User.id.in_(applicant_ids)).all()   # 申请人ID与部门ID
    applicant_department = {row[0]: row[1] for row in applicant_rows}   # 申请人ID→部门ID
    department_manager = {d.id: d.manager_id for d in db.query(Department).all()}   # 部门ID→负责人ID

    # 4.统一角色格式（枚举转字符串），后面按字符串比较
    role = user.role.value if hasattr(user.role, "value") else user.role   # 当前用户角色

    # 5.逐条判断资格：命中一条就 +1，规则与待办列表完全一致
    count = 0                                       # 待办计数
    for expense in pending_expenses:
        # 5.1 取审批流模板，配置异常直接跳过（与待办列表行为一致）
        flow = flow_map.get(expense.flow_id)        # 该单的审批流
        if not flow or not flow.levels:
            continue

        # 5.2 定位单据当前层级要求的角色
        current_level_config = next(                # 当前层级配置
            (level for level in flow.levels if level.get("level") == expense.current_level),
            None                                    # 无匹配层级
        )
        if not current_level_config:
            continue
        required_role = current_level_config.get("role")   # 本级要求的角色

        # 5.3 按本级要求判断该用户是否有资格审批
        if required_role == "manager":
            # 申请人所属部门的负责人就是本人 → 算一条待办
            applicant_department_id = applicant_department.get(expense.user_id)   # 申请人部门ID
            if applicant_department_id is not None and department_manager.get(applicant_department_id) == user.id:
                count += 1                          # 待办加一
        elif required_role == "finance_other":
            # 任一财务，且已在第 1 步排除了本人提交的单据
            if role == "finance":
                count += 1                          # 待办加一
        elif required_role == "finance":
            if role == "finance":
                count += 1                          # 待办加一
        elif required_role == "boss":
            if role == "boss":
                count += 1                          # 待办加一

    return count
