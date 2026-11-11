"""
运维告警检查服务（P1：脚本 + 日志，不接第三方）
    目标：把"没人值守时出了问题也没人知道"的几类情况变成可被脚本判定的告警：
        1. maintenance_runs 失败、或长期卡在 running；
        2. workflow_runs 停留在 running 超过阈值（僵尸工作流）；
        3. ai_review_logs 停留在 pending/running 超过阈值（僵尸预审）；
        4. 当日 token / 费用 / 调用次数超过预算；
        5. 服务探活接口（可选，传入 base_url 才检查）。
    设计约定：
        - 只读库，不修改任何业务数据；
        - 每条告警带稳定的 code（便于抑制与统计）与 severity（critical/warning）；
        - 阈值可显式传入，未传时读配置，便于测试与不同环境调参；
        - 抑制逻辑（filter_suppressed）是纯函数，便于单测，也保证 15 分钟一次的巡检不刷屏。
"""
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIReviewLog,
    AIReviewStatus,
    MaintenanceRun,
    MaintenanceStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.services.cost_service import daily_review_usage



# 严重级别：critical 应立即处理，warning 需当天关注
SEVERITY_CRITICAL = "critical"     # 严重：影响业务或数据安全
SEVERITY_WARNING = "warning"       # 警告：需要关注但可容忍

# 告警代码（稳定标识，用于抑制与统计，不要随意改名）
CODE_MAINTENANCE_FAILED = "maintenance_failed"      # 运维任务失败
CODE_MAINTENANCE_STUCK = "maintenance_stuck"        # 运维任务卡在 running
CODE_WORKFLOW_STUCK = "workflow_stuck"              # 工作流僵尸 run
CODE_REVIEW_STUCK = "ai_review_stuck"               # 预审日志僵尸
CODE_BUDGET_TOKEN = "budget_token_exceeded"         # 超 token 预算
CODE_BUDGET_COST = "budget_cost_exceeded"           # 超费用预算
CODE_BUDGET_CALLS = "budget_calls_exceeded"         # 超调用次数上限
CODE_HEALTH_FAILED = "health_probe_failed"          # 探活失败



# 一条告警
@dataclass(frozen=True)
class Alert:
    """
    单条告警
        :param code: 稳定代码（如 workflow_stuck）
        :param severity: critical / warning
        :param title: 一句话标题
        :param detail: 详细信息（不含敏感内容）
        :param count: 涉及数量（0 表示不适用）
    """
    code: str                                  # 告警代码
    severity: str                              # 级别
    title: str                                 # 标题
    detail: str                                # 详情
    count: int = 0                             # 涉及数量

    def as_dict(self) -> dict:
        """
        转成可序列化的字典
            :return: 字典
        """
        return {
            "code": self.code,                 # 代码
            "severity": self.severity,         # 级别
            "title": self.title,               # 标题
            "detail": self.detail,             # 详情
            "count": self.count,               # 数量
        }



# 运维任务失败 / 卡死
def check_maintenance(db: Session, now: datetime, *, lookback_hours: int = 24,
                      stuck_minutes: int = 60) -> list[Alert]:
    """
    检查运维任务失败与卡死
        :param db: 数据库会话
        :param now: 当前时间
        :param lookback_hours: 回看窗口（小时）
        :param stuck_minutes: running 超过该分钟数视为卡死
        :return: 告警列表
    """
    alerts = []                                                    # 结果
    since = now - timedelta(hours=lookback_hours)                  # 回看起点

    # 1.失败：窗口内所有 failed 记录，带最近一次的错误信息
    failed = (
        db.query(MaintenanceRun)
        .filter(MaintenanceRun.status == MaintenanceStatus.FAILED,
                MaintenanceRun.started_at >= since)
        .order_by(MaintenanceRun.started_at.desc())
        .all()
    )
    if failed:
        latest = failed[0]                                         # 最近一次失败
        alerts.append(Alert(
            code=CODE_MAINTENANCE_FAILED,
            severity=SEVERITY_CRITICAL,
            title="运维任务执行失败",
            detail=(
                f"最近 {lookback_hours} 小时有 {len(failed)} 次失败，"
                f"最新 job={latest.job} 错误={str(latest.error_message or '')[:200]}"
            ),
            count=len(failed),
        ))

    # 2.卡死：running 且开始时间早于阈值（进程崩溃会留下这种记录）
    stuck_before = now - timedelta(minutes=stuck_minutes)          # 卡死阈值
    stuck = (
        db.query(MaintenanceRun)
        .filter(MaintenanceRun.status == MaintenanceStatus.RUNNING,
                MaintenanceRun.started_at < stuck_before)
        .all()
    )
    if stuck:
        names = ",".join(sorted({run.job for run in stuck}))       # 涉及的 job 名
        alerts.append(Alert(
            code=CODE_MAINTENANCE_STUCK,
            severity=SEVERITY_WARNING,
            title="运维任务长时间处于运行中",
            detail=f"超过 {stuck_minutes} 分钟仍为 running：{names}",
            count=len(stuck),
        ))
    return alerts



# 僵尸工作流
def check_stale_workflows(db: Session, now: datetime, *, stale_minutes: int) -> list[Alert]:
    """
    检查卡在 running 的工作流
        :param db: 数据库会话
        :param now: 当前时间
        :param stale_minutes: 超时分钟数
        :return: 告警列表
    """
    before = now - timedelta(minutes=stale_minutes)                # 阈值
    runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.status == WorkflowRunStatus.RUNNING,
                WorkflowRun.started_at < before)
        .all()
    )
    if not runs:
        return []
    ids = ",".join(str(run.id) for run in runs[:20])               # 最多列 20 个ID
    return [Alert(
        code=CODE_WORKFLOW_STUCK,
        severity=SEVERITY_WARNING,
        title="工作流长时间处于 running",
        detail=f"超过 {stale_minutes} 分钟未收尾，run_id={ids}；可用 scripts/recover_workflows.py 收尾",
        count=len(runs),
    )]



# 僵尸预审
def check_stale_reviews(db: Session, now: datetime, *, stale_minutes: int) -> list[Alert]:
    """
    检查卡在 pending/running 的预审日志
        :param db: 数据库会话
        :param now: 当前时间
        :param stale_minutes: 超时分钟数
        :return: 告警列表
    """
    before = now - timedelta(minutes=stale_minutes)                # 阈值
    logs = (
        db.query(AIReviewLog)
        .filter(AIReviewLog.status.in_([AIReviewStatus.PENDING, AIReviewStatus.RUNNING]),
                AIReviewLog.created_at < before)
        .all()
    )
    if not logs:
        return []
    ids = ",".join(str(log.id) for log in logs[:20])               # 最多列 20 个ID
    return [Alert(
        code=CODE_REVIEW_STUCK,
        severity=SEVERITY_WARNING,
        title="AI 预审长时间未完成",
        detail=f"超过 {stale_minutes} 分钟仍为 pending/running，review_id={ids}",
        count=len(logs),
    )]



# 预算告警
def check_budget(db: Session, *, usage: dict | None = None) -> list[Alert]:
    """
    检查当日用量是否触及预算
        :param db: 数据库会话
        :param usage: 已统计好的当日用量（不传则现算）
        :return: 告警列表
    """
    data = usage or daily_review_usage(db)                         # 当日用量
    alerts = []                                                    # 结果

    # 1.token 预算
    token_limit = int(settings.AI_REVIEW_DAILY_TOKEN_LIMIT or 0)   # 配置上限
    if token_limit > 0 and data["tokens"] >= token_limit:
        alerts.append(Alert(
            code=CODE_BUDGET_TOKEN,
            severity=SEVERITY_CRITICAL,
            title="当日 token 用量已达预算",
            detail=f"已用 {data['tokens']}，上限 {token_limit}",
            count=data["tokens"],
        ))

    # 2.费用预算
    cost_limit = Decimal(str(settings.AI_REVIEW_DAILY_COST_LIMIT or 0))   # 配置上限
    if cost_limit > 0 and Decimal(str(data["cost"])) >= cost_limit:
        alerts.append(Alert(
            code=CODE_BUDGET_COST,
            severity=SEVERITY_CRITICAL,
            title="当日费用已达预算",
            detail=f"已用 {data['cost']} 元，上限 {cost_limit} 元",
            count=1,
        ))

    # 3.调用次数上限
    call_limit = int(settings.AI_REVIEW_DAILY_LIMIT or 0)          # 配置上限
    if call_limit > 0 and data["calls"] >= call_limit:
        alerts.append(Alert(
            code=CODE_BUDGET_CALLS,
            severity=SEVERITY_WARNING,
            title="当日预审调用次数已达上限",
            detail=f"已调用 {data['calls']} 次，上限 {call_limit} 次",
            count=data["calls"],
        ))
    return alerts



# 探活
def check_http_health(base_url: str, *, timeout: float = 5.0) -> Alert | None:
    """
    调用 /health 做探活
        :param base_url: 服务地址（如 http://127.0.0.1:8000）
        :param timeout: 超时秒数
        :return: 失败时返回告警，成功返回 None
    """
    url = base_url.rstrip("/") + "/health"                         # 探活地址
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:   # 发请求
            body = response.read(200).decode("utf-8", "replace")     # 读前 200 字节
            if response.status == 200 and "healthy" in body:       # 正常
                return None
            return Alert(
                code=CODE_HEALTH_FAILED,
                severity=SEVERITY_CRITICAL,
                title="健康检查返回异常",
                detail=f"HTTP {response.status} body={body[:120]}",
            )
    except (urllib.error.URLError, OSError, ValueError) as exc:     # 连接失败/格式异常
        return Alert(
            code=CODE_HEALTH_FAILED,
            severity=SEVERITY_CRITICAL,
            title="健康检查请求失败",
            detail=f"{type(exc).__name__}: {str(exc)[:160]}",
        )



# 汇总检查
def collect_alerts(db: Session, *, now: datetime | None = None, base_url: str | None = None,
                   lookback_hours: int = 24, maintenance_stuck_minutes: int = 60,
                   workflow_stale_minutes: int | None = None,
                   review_stale_minutes: int | None = None) -> list[Alert]:
    """
    收集全部告警
        :param db: 数据库会话
        :param now: 当前时间（测试可注入）
        :param base_url: 传入则额外做探活
        :param lookback_hours: 运维失败回看窗口
        :param maintenance_stuck_minutes: 运维任务卡死阈值
        :param workflow_stale_minutes: 工作流超时阈值（不传读配置）
        :param review_stale_minutes: 预审超时阈值（不传读配置）
        :return: 告警列表（顺序固定，便于日志比对）
    """
    moment = now or datetime.now()                                 # 当前时间
    workflow_minutes = workflow_stale_minutes or settings.WORKFLOW_STALE_MINUTES   # 工作流阈值
    review_minutes = review_stale_minutes or settings.AI_REVIEW_STALE_MINUTES      # 预审阈值

    alerts = []                                                    # 结果
    alerts.extend(check_maintenance(
        db, moment, lookback_hours=lookback_hours, stuck_minutes=maintenance_stuck_minutes
    ))
    alerts.extend(check_stale_workflows(db, moment, stale_minutes=workflow_minutes))
    alerts.extend(check_stale_reviews(db, moment, stale_minutes=review_minutes))
    alerts.extend(check_budget(db))
    if base_url:                                                   # 仅在指定地址时探活
        health = check_http_health(base_url)
        if health is not None:
            alerts.append(health)
    return alerts



# 告警抑制
def filter_suppressed(alerts: list[Alert], state: dict, *, now: datetime,
                      suppress_minutes: int = 60) -> tuple[list[Alert], dict]:
    """
    按"同一代码在抑制窗口内只报一次"过滤告警
        - 级别升级（warning → critical）时不受抑制窗口限制，避免严重问题被静音；
        - 返回值里的 state 需要调用方持久化（脚本用 JSON 文件）。
        :param alerts: 本次收集到的告警
        :param state: 上次的状态 {code: {"severity":..., "last_emitted": ISO 时间}}
        :param now: 当前时间
        :param suppress_minutes: 抑制窗口（分钟，<=0 表示不抑制）
        :return: (需要发出的告警, 新状态)
    """
    emitted = []                                                   # 需要发出的告警
    new_state = dict(state or {})                                  # 新状态

    for alert in alerts:
        previous = new_state.get(alert.code)                       # 该代码的上次记录
        should_emit = True                                         # 默认发出

        if previous and suppress_minutes > 0:
            severity_escalated = (                                 # 级别是否升级
                previous.get("severity") == SEVERITY_WARNING and alert.severity == SEVERITY_CRITICAL
            )
            last_raw = previous.get("last_emitted")                # 上次发出时间
            elapsed_enough = True
            if last_raw:
                try:
                    elapsed_enough = (now - datetime.fromisoformat(last_raw)) >= timedelta(minutes=suppress_minutes)
                except ValueError:                                 # 时间格式坏了，按需发出处理
                    elapsed_enough = True
            should_emit = severity_escalated or elapsed_enough     # 升级或窗口已过才发

        if not should_emit:
            continue

        emitted.append(alert)                                      # 记录待发
        new_state[alert.code] = {
            "severity": alert.severity,                            # 记录级别
            "last_emitted": now.isoformat(timespec="seconds"),     # 记录时间
        }

    return emitted, new_state
