"""
模型包统一导出（app/models）
    集中导入全部 ORM 模型并声明 __all__，承担两个职责：
        1. 保证所有模型都在同一套 Base.metadata 上完成注册。relationship 里用的是字符串类名，
           只有相关类都被导入过，SQLAlchemy 才能解析到，否则会报 "failed to locate" 之类的映射错误；
        2. 给业务层一个统一入口，直接 `from app.models import User, Expense, ApprovalStatus`，
           不必关心每个类具体在哪个文件。
    维护约定：新增模型时，在本文件补一行导入，并把类名加入 __all__，否则跨模块引用可能因未注册而失败。
"""
from app.models.base import Base
from app.models.user import User, UserRole
from app.models.expense import Expense, ExpenseItem, Category
from app.models.approval import ApprovalFlow, Approval, ApprovalStatus
from app.models.rule import Rule, ExpenseRuleHit
from app.models.department import Department
from app.models.attachment import Attachment
from app.models.ai_review import AIReviewLog, AIReviewStatus
from app.models.document import (
    DOCUMENT_TYPES,
    DOCUMENT_TYPE_EVIDENCE,
    EVIDENCE_ORDER,
    DocumentExtraction,
    DocumentExtractionStatus,
)
from app.models.workflow import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from app.models.checkpoint import (
    WorkflowCheckpoint,
    WorkflowCheckpointBlob,
    WorkflowCheckpointWrite,
)
from app.models.maintenance import MaintenanceRun, MaintenanceStatus
from app.models.settlement import ExpenseSettlement
from app.models.notification import Notification
from app.models.agent import AgentInvestigation, AgentInvestigationStatus


__all__ = ["Base", "User", "UserRole", "Expense", "ExpenseItem",   # 对外导出的模型清单
           "Category", "ApprovalFlow", "Approval", "ApprovalStatus", "Rule","Department",
           "Attachment", "ExpenseRuleHit", "AIReviewLog", "AIReviewStatus",
           "DocumentExtraction", "DocumentExtractionStatus", "DOCUMENT_TYPES",
           "DOCUMENT_TYPE_EVIDENCE", "EVIDENCE_ORDER",
           "WorkflowRun", "WorkflowRunStatus", "WorkflowStep", "WorkflowStepStatus",
           "WorkflowCheckpoint", "WorkflowCheckpointBlob", "WorkflowCheckpointWrite",
           "MaintenanceRun", "MaintenanceStatus", "ExpenseSettlement", "Notification",
           "AgentInvestigation", "AgentInvestigationStatus"]
