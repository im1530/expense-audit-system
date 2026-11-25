"""AI 核查 Agent 的离线测试：ReAct 轨迹解析、记录落库、只读工具。"""
import asyncio
import unittest

import tests  # noqa: F401
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.investigation_agent import InvestigationAgent, build_tools
from app.models import AgentInvestigation, AgentInvestigationStatus
from app.models.expense import ExpenseStatus
from app.services.agent_service import investigate_expense
from tests.support import BaseTestCase, make_expense


class FakeGraph:
    """不调用 LLM 的 ReAct 图：模拟一次工具调用 + 最终报告"""

    def __init__(self, expense_id: int):
        self.expense_id = expense_id

    async def ainvoke(self, payload, config):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "get_expense_overview",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content='{"expense": {"expense_no": "EXP-TEST"}}',
                    tool_call_id="call-1",
                    name="get_expense_overview",
                ),
                AIMessage(content="核查完成：该单金额与制度条款一致，暂未发现异常。"),
            ]
        }


class InvestigationAgentTest(BaseTestCase, unittest.TestCase):
    """Agent 结果解析与持久化"""

    def pending_expense(self):
        employee = self.fx["users"]["employee"]
        return make_expense(
            self.db,
            employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount="100.00",
            category_id=self.fx["categories"]["rail"].id,
        )

    def test_parse_tool_trace_and_report(self):
        """工具调用与最终报告能被解析成结构化轨迹"""
        expense = self.pending_expense()
        agent = InvestigationAgent(self.Session, expense_id=expense.id, graph=FakeGraph(expense.id))
        result = asyncio.run(agent.run("请核查"))

        self.assertIn("核查完成", result["report"])
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["trace"][0]["tool"], "get_expense_overview")
        self.assertEqual(result["trace"][0]["status"], "success")
        self.assertIn("EXP-TEST", result["trace"][0]["result"])

    def test_investigation_is_persisted(self):
        """服务层把成功结果写入 agent_investigations"""
        expense = self.pending_expense()
        agent = InvestigationAgent(self.Session, expense_id=expense.id, graph=FakeGraph(expense.id))
        record = asyncio.run(investigate_expense(
            self.db,
            expense=expense,
            user=self.fx["users"]["employee"],
            agent=agent,
        ))

        self.assertEqual(record.status, AgentInvestigationStatus.SUCCESS)
        self.assertIn("核查完成", record.report)
        self.assertEqual(record.trace[0]["tool"], "get_expense_overview")
        stored = self.db.query(AgentInvestigation).filter(AgentInvestigation.id == record.id).one()
        self.assertEqual(stored.status, "success")

    def test_tools_are_read_only_and_return_json(self):
        """只读工具可以直接被 ReAct 调用，返回 JSON 字符串"""
        expense = self.pending_expense()
        tools = {tool.name: tool for tool in build_tools(self.Session, expense_id=expense.id)}
        raw = tools["get_expense_overview"].invoke({})
        self.assertIn("expense", raw)
        self.assertIn(expense.expense_no, raw)

    def test_tools_do_not_expose_expense_id_to_model(self):
        """安全边界：单据ID由服务端闭包绑定，工具参数里不能出现 expense_id"""
        expense = self.pending_expense()
        tools = build_tools(self.Session, expense_id=expense.id)
        for item in tools:
            self.assertNotIn("expense_id", item.args, item.name)


if __name__ == "__main__":
    unittest.main()
