"""
HTTP 契约测试（离线）
    目的：补一层函数级单测覆盖不到的 HTTP 行为：
        - 鉴权中间件顺序：无 token 必须 401，公开接口不受影响；
        - 参数校验错误码：422 + VALIDATION_ERROR；
        - 成功响应的状态码与关键字段；
        - 响应模型裁剪：不能把 password_hash 等内部字段带出去。
"""
import unittest

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from fastapi.testclient import TestClient

from app.main import app
from tests.support import TEST_PASSWORD, BaseTestCase


class HttpContractTest(BaseTestCase, unittest.TestCase):
    """状态码 + 关键字段的 HTTP 契约"""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app, base_url="http://localhost")

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def _login(self, username: str = "employee_e1") -> str:
        """登录并返回 Bearer token"""
        response = self.client.post(
            "/api/v1/auth/login",
            data={"username": username, "password": TEST_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["token_type"], "bearer")
        self.assertTrue(payload["access_token"])
        return payload["access_token"]

    def test_health_and_auth_contract(self):
        """健康检查公开；无 token 401；登录返回 bearer token；响应不含密码哈希"""
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "healthy"})

        unauthorized = self.client.get("/api/v1/auth/me")
        self.assertEqual(unauthorized.status_code, 401)

        bad_login = self.client.post(
            "/api/v1/auth/login",
            data={"username": "employee_e1", "password": "wrong-password"},
        )
        self.assertEqual(bad_login.status_code, 401)

        token = self._login()
        me = self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me.status_code, 200)
        body = me.json()
        self.assertEqual(body["username"], "employee_e1")
        self.assertNotIn("password_hash", body)
        self.assertNotIn("password", body)

    def test_create_expense_contract(self):
        """创建草稿：200 + 关键字段齐全；金额/状态由服务端生成"""
        token = self._login()
        response = self.client.post(
            "/api/v1/expense/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "title": "HTTP 契约测试",
                "description": "创建草稿",
                "items": [{
                    "category_id": self.fx["categories"]["rail"].id,
                    "description": "高铁票",
                    "amount": "100.00",
                    "expense_date": "2026-11-01",
                }],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        for key in ("id", "expense_no", "title", "status", "total_amount", "items"):
            self.assertIn(key, body)
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["total_amount"], "100.00")
        self.assertEqual(len(body["items"]), 1)
        self.assertNotIn("password_hash", str(body))

    def test_validation_error_contract(self):
        """请求体不合法：422 + 机器可读错误码"""
        token = self._login()
        response = self.client.post(
            "/api/v1/expense/",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "空明细", "items": []},
        )
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body.get("code"), "VALIDATION_ERROR")
        self.assertEqual(body.get("message"), "请求参数校验失败")
        self.assertTrue(body.get("errors"))


if __name__ == "__main__":
    unittest.main()
