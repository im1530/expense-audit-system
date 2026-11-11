"""
运维告警检查的离线单测（不联网；使用测试库）
    覆盖：
        - maintenance_runs 失败 / 卡在 running；
        - workflow_runs 僵尸 run；
        - ai_review_logs 僵尸记录；
        - 当日 token / 费用 / 调用次数预算；
        - 探活成功与失败（用 mock 替换 urlopen，不发真实请求）；
        - 告警抑制：窗口内不重复、级别升级不受抑制。
"""
import unittest
import urllib.error
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.config import settings
from app.models import (
    AIReviewLog,
    AIReviewStatus,
    MaintenanceRun,
    MaintenanceStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.services.alert_service import (
    CODE_BUDGET_COST,
    CODE_BUDGET_TOKEN,
    CODE_HEALTH_FAILED,
    CODE_MAINTENANCE_FAILED,
    CODE_MAINTENANCE_STUCK,
    CODE_REVIEW_STUCK,
    CODE_WORKFLOW_STUCK,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Alert,
    check_budget,
    check_http_health,
    check_maintenance,
    check_stale_reviews,
    check_stale_workflows,
    collect_alerts,
    filter_suppressed,
)
from tests.support import BaseTestCase, make_expense



# 造一条运维任务记录
def make_maintenance(session, *, job: str, status: MaintenanceStatus,
                     started_at: datetime, error: str | None = None) -> MaintenanceRun:
    """
    创建一条 maintenance_runs 记录
        :param session: 会话
        :param job: 任务名
        :param status: 状态
        :param started_at: 开始时间
        :param error: 失败原因
        :return: 记录对象
    """
    run = MaintenanceRun(
        job=job,                                                   # 任务名
        status=status,                                             # 状态
        started_at=started_at,                                     # 开始时间
        finished_at=None if status == MaintenanceStatus.RUNNING else datetime.now(),   # 结束时间
        summary={"dry_run": False},                                # 汇总
        error_message=error,                                       # 错误
    )
    session.add(run)
    session.commit()
    return run



class MaintenanceAlertTest(BaseTestCase, unittest.TestCase):
    """运维任务告警"""

    def test_failed_run_raises_critical(self):
        """窗口内存在失败记录时给 critical"""
        now = datetime.now()
        make_maintenance(self.db, job="retention_purge", status=MaintenanceStatus.FAILED,
                         started_at=now - timedelta(hours=2), error="db timeout")
        alerts = check_maintenance(self.db, now, lookback_hours=24, stuck_minutes=60)
        codes = [alert.code for alert in alerts]
        self.assertIn(CODE_MAINTENANCE_FAILED, codes)
        failed = next(alert for alert in alerts if alert.code == CODE_MAINTENANCE_FAILED)
        self.assertEqual(failed.severity, SEVERITY_CRITICAL)
        self.assertIn("db timeout", failed.detail)

    def test_success_run_no_alert(self):
        """只有成功记录时不告警"""
        now = datetime.now()
        make_maintenance(self.db, job="retention_purge", status=MaintenanceStatus.SUCCESS,
                         started_at=now - timedelta(hours=1))
        self.assertEqual(check_maintenance(self.db, now), [])

    def test_failed_outside_lookback_ignored(self):
        """超出回看窗口的失败不再告警"""
        now = datetime.now()
        make_maintenance(self.db, job="retention_purge", status=MaintenanceStatus.FAILED,
                         started_at=now - timedelta(hours=48), error="old failure")
        self.assertEqual(check_maintenance(self.db, now, lookback_hours=24), [])

    def test_stuck_running_raises_warning(self):
        """running 超过阈值给 warning"""
        now = datetime.now()
        make_maintenance(self.db, job="workflow_recover", status=MaintenanceStatus.RUNNING,
                         started_at=now - timedelta(hours=3))
        alerts = check_maintenance(self.db, now, stuck_minutes=60)
        codes = [alert.code for alert in alerts]
        self.assertIn(CODE_MAINTENANCE_STUCK, codes)
        stuck = next(alert for alert in alerts if alert.code == CODE_MAINTENANCE_STUCK)
        self.assertEqual(stuck.severity, SEVERITY_WARNING)



class WorkflowAlertTest(BaseTestCase, unittest.TestCase):
    """僵尸工作流告警"""

    def _make_run(self, *, minutes_old: int, status: WorkflowRunStatus = WorkflowRunStatus.RUNNING) -> WorkflowRun:
        """造一条工作流 run"""
        employee = self.fx["users"]["employee"]                    # 提交人
        expense = make_expense(self.db, employee)                  # 单据
        run = WorkflowRun(
            expense_id=expense.id,                                 # 归属单据
            status=status,                                         # 状态
            started_at=datetime.now() - timedelta(minutes=minutes_old),   # 开始时间
            current_node="risk",                                   # 当前节点
        )
        self.db.add(run)
        self.db.commit()
        return run

    def test_stale_running_detected(self):
        """超时仍在 running 的工作流被检出"""
        self._make_run(minutes_old=30)
        alerts = check_stale_workflows(self.db, datetime.now(), stale_minutes=15)
        self.assertEqual([alert.code for alert in alerts], [CODE_WORKFLOW_STUCK])
        self.assertEqual(alerts[0].count, 1)

    def test_fresh_running_not_detected(self):
        """未超时的不算僵尸"""
        self._make_run(minutes_old=1)
        self.assertEqual(check_stale_workflows(self.db, datetime.now(), stale_minutes=15), [])

    def test_finished_run_not_detected(self):
        """已成功结束的不算僵尸"""
        self._make_run(minutes_old=120, status=WorkflowRunStatus.SUCCESS)
        self.assertEqual(check_stale_workflows(self.db, datetime.now(), stale_minutes=15), [])



class ReviewAlertTest(BaseTestCase, unittest.TestCase):
    """僵尸预审告警"""

    def _make_log(self, *, minutes_old: int, status: AIReviewStatus) -> AIReviewLog:
        """造一条预审日志"""
        employee = self.fx["users"]["employee"]                    # 触发人
        expense = make_expense(self.db, employee)                  # 单据
        log = AIReviewLog(
            expense_id=expense.id,                                 # 归属单据
            triggered_by=employee.id,                              # 触发人
            status=status,                                         # 状态
            created_at=datetime.now() - timedelta(minutes=minutes_old),   # 创建时间
        )
        self.db.add(log)
        self.db.commit()
        return log

    def test_stale_pending_detected(self):
        """超时仍 pending 的预审被检出"""
        self._make_log(minutes_old=30, status=AIReviewStatus.PENDING)
        alerts = check_stale_reviews(self.db, datetime.now(), stale_minutes=10)
        self.assertEqual([alert.code for alert in alerts], [CODE_REVIEW_STUCK])

    def test_stale_running_detected(self):
        """超时仍 running 的预审被检出"""
        self._make_log(minutes_old=30, status=AIReviewStatus.RUNNING)
        self.assertEqual(len(check_stale_reviews(self.db, datetime.now(), stale_minutes=10)), 1)

    def test_done_review_ignored(self):
        """已完成的预审不算僵尸"""
        self._make_log(minutes_old=120, status=AIReviewStatus.SUCCESS)
        self.assertEqual(check_stale_reviews(self.db, datetime.now(), stale_minutes=10), [])



class BudgetAlertTest(BaseTestCase, unittest.TestCase):
    """预算告警"""

    def _make_usage_log(self, *, prompt_tokens: int, completion_tokens: int = 0) -> None:
        """造一条带 token 用量的预审日志（当天）"""
        employee = self.fx["users"]["employee"]                    # 触发人
        expense = make_expense(self.db, employee)                  # 单据
        log = AIReviewLog(
            expense_id=expense.id,                                 # 归属单据
            status=AIReviewStatus.SUCCESS,                         # 状态
            prompt_tokens=prompt_tokens,                           # 输入 token
            completion_tokens=completion_tokens,                   # 输出 token
            created_at=datetime.now(),                             # 当天
        )
        self.db.add(log)
        self.db.commit()

    def test_token_budget_exceeded(self):
        """token 用量达到上限时告警"""
        self._make_usage_log(prompt_tokens=50)
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 10):
            alerts = check_budget(self.db)
        self.assertEqual([alert.code for alert in alerts], [CODE_BUDGET_TOKEN])
        self.assertEqual(alerts[0].severity, SEVERITY_CRITICAL)

    def test_cost_budget_exceeded(self):
        """费用达到上限时告警"""
        self._make_usage_log(prompt_tokens=1_000_000)
        with patch.object(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0), \
                patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.5):
            alerts = check_budget(self.db)
        self.assertEqual([alert.code for alert in alerts], [CODE_BUDGET_COST])
        self.assertEqual(alerts[0].severity, SEVERITY_CRITICAL)

    def test_below_budget_no_alert(self):
        """未达预算不告警"""
        self._make_usage_log(prompt_tokens=5)
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 1000), \
                patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.0):
            self.assertEqual(check_budget(self.db), [])

    def test_limits_disabled_no_alert(self):
        """预算为 0（未配置）时一律不告警"""
        self._make_usage_log(prompt_tokens=10_000_000)
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 0), \
                patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.0), \
                patch.object(settings, "AI_REVIEW_DAILY_LIMIT", 0):
            self.assertEqual(check_budget(self.db), [])



class HealthProbeTest(unittest.TestCase):
    """探活（mock 掉网络请求）"""

    class _FakeResponse:
        """模拟 urlopen 返回的响应对象"""

        def __init__(self, status: int, body: bytes):
            self.status = status                               # HTTP 状态码
            self._body = body                                  # 响应体

        def read(self, size: int | None = None) -> bytes:      # noqa: ARG002 - 保持与真实对象签名一致
            """返回响应体"""
            return self._body

        def __enter__(self):
            """支持 with 语法"""
            return self

        def __exit__(self, exc_type, exc, tb):                 # noqa: ARG002
            """退出 with"""
            return False

    def test_healthy_returns_none(self):
        """200 且含 healthy 视为正常"""
        response = self._FakeResponse(200, b'{"status":"healthy"}')
        with patch("app.services.alert_service.urllib.request.urlopen", return_value=response):
            self.assertIsNone(check_http_health("http://127.0.0.1:8000"))

    def test_bad_status_returns_alert(self):
        """非 200 或内容异常时告警"""
        response = self._FakeResponse(500, b'{"detail":"boom"}')
        with patch("app.services.alert_service.urllib.request.urlopen", return_value=response):
            alert = check_http_health("http://127.0.0.1:8000")
        self.assertIsNotNone(alert)
        self.assertEqual(alert.code, CODE_HEALTH_FAILED)
        self.assertEqual(alert.severity, SEVERITY_CRITICAL)

    def test_connection_error_returns_alert(self):
        """连接失败时告警而不是抛异常"""
        with patch("app.services.alert_service.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            alert = check_http_health("http://127.0.0.1:8000")
        self.assertEqual(alert.code, CODE_HEALTH_FAILED)



class SuppressionTest(unittest.TestCase):
    """告警抑制（纯函数）"""

    def _alert(self, code: str = "x", severity: str = SEVERITY_WARNING) -> Alert:
        """造一条告警"""
        return Alert(code=code, severity=severity, title="t", detail="d", count=1)

    def test_first_time_emitted(self):
        """首次出现的告警正常发出"""
        now = datetime.now()
        emitted, state = filter_suppressed([self._alert()], {}, now=now, suppress_minutes=60)
        self.assertEqual(len(emitted), 1)
        self.assertIn("x", state)

    def test_same_code_suppressed_within_window(self):
        """窗口内同一代码不再重复发出"""
        now = datetime.now()
        emitted, state = filter_suppressed([self._alert()], {}, now=now, suppress_minutes=60)
        self.assertEqual(len(emitted), 1)
        again, _ = filter_suppressed([self._alert()], state, now=now + timedelta(minutes=5), suppress_minutes=60)
        self.assertEqual(again, [])

    def test_emitted_again_after_window(self):
        """超过抑制窗口后再次发出"""
        now = datetime.now()
        _, state = filter_suppressed([self._alert()], {}, now=now, suppress_minutes=60)
        again, _ = filter_suppressed([self._alert()], state, now=now + timedelta(minutes=61), suppress_minutes=60)
        self.assertEqual(len(again), 1)

    def test_severity_escalation_not_suppressed(self):
        """warning 升级为 critical 时立即发出，不被窗口挡住"""
        now = datetime.now()
        _, state = filter_suppressed([self._alert()], {}, now=now, suppress_minutes=60)
        escalated, _ = filter_suppressed(
            [self._alert(severity=SEVERITY_CRITICAL)], state, now=now + timedelta(minutes=5), suppress_minutes=60
        )
        self.assertEqual(len(escalated), 1)

    def test_zero_window_disables_suppression(self):
        """窗口为 0 表示不抑制"""
        now = datetime.now()
        _, state = filter_suppressed([self._alert()], {}, now=now, suppress_minutes=60)
        emitted, _ = filter_suppressed([self._alert()], state, now=now, suppress_minutes=0)
        self.assertEqual(len(emitted), 1)



class CollectAlertsTest(BaseTestCase, unittest.TestCase):
    """汇总入口"""

    def test_clean_database_has_no_alert(self):
        """干净库（无失败/无僵尸/未超预算）不产生告警"""
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 0), \
                patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.0), \
                patch.object(settings, "AI_REVIEW_DAILY_LIMIT", 0):
            alerts = collect_alerts(self.db, now=datetime.now())
        self.assertEqual(alerts, [])

    def test_multiple_sources_combined(self):
        """多类问题同时出现时都进汇总"""
        now = datetime.now()
        make_maintenance(self.db, job="retention_purge", status=MaintenanceStatus.FAILED,
                         started_at=now - timedelta(hours=1), error="boom")
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee)
        self.db.add(WorkflowRun(expense_id=expense.id, status=WorkflowRunStatus.RUNNING,
                                started_at=now - timedelta(hours=2), current_node="risk"))
        self.db.commit()
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 0), \
                patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.0):
            alerts = collect_alerts(self.db, now=now)
        codes = {alert.code for alert in alerts}
        self.assertIn(CODE_MAINTENANCE_FAILED, codes)
        self.assertIn(CODE_WORKFLOW_STUCK, codes)



if __name__ == "__main__":
    unittest.main()
