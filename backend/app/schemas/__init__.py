"""
数据模型包（app/schemas）
    Pydantic 模型层，负责 HTTP 请求入参校验与响应出参序列化，与 models 层的 ORM 表结构一一对应但职责不同：
        - common.py      通用分页参数 PageParams、通用业务筛选 BusinessFilter
        - user.py        用户注册/登录入参、AI 数据处理授权入参、用户信息与下拉选项出参
        - department.py  部门下拉选项出参
        - expense.py     报销单/明细/费用分类/字典选项的全部入参与出参模型
        - approval.py    审批动作入参、审批记录出参、审批轨迹展示项
        - ai_review.py   AI 预审入参/出参/制度引用
        - document.py    凭证识别草稿与响应模型
    出参模型统一启用 `from_attributes=True`，可直接把 SQLAlchemy 实体交给 response_model 序列化，
    由 FastAPI 负责字段过滤，因此密码等敏感字段不会泄露。
    本包不做集中再导出，使用方按 `from app.schemas.xxx import Yyy` 直接引入，避免循环导入。
"""
