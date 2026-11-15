"""
real_self_v1 凭证识别小样本试点（真实调用多模态模型，3~5 条）

    做什么：
        1. 对含个人信息的凭证先做最小化遮蔽（姓名/微信号/订单号/余额）；
        2. 调用系统真实链路 `prepare_attachment → DocumentAgent.extract`；
        3. 把识别结果与人工标注的期望字段逐项对比，输出 JSON 报告；
        4. 不写业务库、不落附件、不保存模型原始返回。

    用法（在 backend/ 下）：
        python evaluation/ocr_pilot_real_self.py

    成本：默认 4 条（2 张发票 + 1 张支付凭证 + 1 张支付截图），约几千 token。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw
import pypdfium2 as pdfium

from app.agents.document_agent import DocumentAgent, DocumentAgentError
from app.services.document_preprocess import prepare_attachment


# 仓库内路径
BASE = Path(__file__).resolve().parent
# 原始件目录（仓库外）
RAW = Path(r"D:\expense_real_self\raw")
# 脱敏件目录（仓库外）
ASSETS = Path(r"D:\expense_real_self\assets")
# 报告输出
OUT = BASE / "reports" / "real_self_v1_ocr.json"

# 试点案例：case_id → (原始文件, 脱敏后文件, 内容类型, 遮罩矩形, 期望)
CASES = [
    {
        "case_id": "R003",
        "source": "办公室办公器材.pdf",
        "redacted": None,
        "content_type": "application/pdf",
        "rects": [],
        "expect_type": "vat_invoice",
        "expect_amount": "73.90",
        "expect_date": "2026-11-05",
        "expect_party": "晶东",
    },
    {
        "case_id": "R005",
        "source": "员工餐_麦当劳.pdf",
        "redacted": None,
        "content_type": "application/pdf",
        "rects": [],
        "expect_type": "vat_invoice",
        "expect_amount": "56.60",
        "expect_date": "2026-11-15",
        "expect_party": "麦当劳",
    },
    {
        "case_id": "R002",
        "source": "停车费.pdf",
        "redacted": "R002_redacted.png",
        "content_type": "application/pdf",
        "rects": [(280, 430, 820, 525)],   # 付款方微信号/姓名
        "expect_type": "payment_screenshot",
        "expect_amount": "18.00",
        "expect_date": "2026-11-02",
        "expect_party": "停车",
    },
    {
        "case_id": "R010",
        "source": "员工餐_支付截图形式.jpg",
        "redacted": "R010_redacted.jpg",
        "content_type": "image/jpeg",
        # 只盖转账单号与零钱余额；转账时间必须保留，否则日期抽不出来
        "rects": [(40, 1320, 1130, 1470), (250, 1460, 1130, 1590)],
        "expect_type": "payment_screenshot",
        "expect_amount": "8.00",
        "expect_date": "2026-11-11",
        "expect_party": "热干面",
    },
    {
        "case_id": "R001",
        "source": "AI办公工具额度.pdf",
        "redacted": "R001_redacted.png",
        "content_type": "application/pdf",
        "rects": [(280, 430, 820, 525)],   # 付款方微信号/姓名
        "expect_type": "payment_screenshot",
        "expect_amount": "50.00",
        "expect_date": "2026-11-04",
        "expect_party": "深度求索",
    },
    {
        "case_id": "R004",
        "source": "办公室打印.pdf",
        "redacted": "R004_redacted.png",
        "content_type": "application/pdf",
        "rects": [(280, 430, 820, 525)],   # 付款方微信号/姓名
        "expect_type": "payment_screenshot",
        "expect_amount": "2.00",
        "expect_date": "2026-10-17",
        "expect_party": "打印",
    },
    {
        "case_id": "R006",
        "source": "员工餐_麦当劳2.pdf",
        "redacted": None,
        "content_type": "application/pdf",
        "rects": [],
        "expect_type": "vat_invoice",
        "expect_amount": "33.40",
        "expect_date": "2026-11-15",
        "expect_party": "麦当劳",
    },
    {
        "case_id": "R007",
        "source": "滴滴电子发票A.pdf",
        "redacted": "R007_redacted.png",
        "content_type": "application/pdf",
        "rects": [(40, 470, 340, 515)],    # 出行人姓名 + 证件号
        "expect_type": "vat_invoice",
        "expect_amount": "12.40",
        "expect_date": "2026-10-08",
        "expect_party": "滴滴",
    },
    {
        "case_id": "R008",
        "source": "滴滴电子发票B.pdf",
        "redacted": "R008_redacted.png",
        "content_type": "application/pdf",
        "rects": [(40, 470, 340, 560)],    # 两行出行人姓名 + 证件号
        "expect_type": "vat_invoice",
        "expect_amount": "30.50",
        "expect_date": "2026-10-05",
        "expect_dates": ["2026-10-04", "2026-10-05"],  # 两段行程，取任一天都算对
        "expect_party": "滴滴",
    },
    {
        "case_id": "R009",
        "source": "高铁票电子发票.pdf",
        "redacted": "R009_redacted.png",
        "content_type": "application/pdf",
        "rects": [(40, 430, 540, 480), (40, 580, 400, 630)],  # 证件号+姓名、购买方姓名
        "expect_type": "train_ticket",
        "expect_amount": "34.50",
        "expect_date": "2026-10-08",
        "expect_party": "铁路",
    },
]


def redact_pdf(source: Path, target: Path, rects: list[tuple[int, int, int, int]]) -> None:
    """
    把 PDF 首页渲染成 PNG 并按矩形涂白（只用于外发识别，不改原件）
        :param source: 原 PDF
        :param target: 输出 PNG
        :param rects: 像素矩形 (x1, y1, x2, y2)
    """
    document = pdfium.PdfDocument(str(source))  # 打开 PDF
    image = document[0].render(scale=2.0).to_pil().convert("RGB")  # 渲染首页
    draw = ImageDraw.Draw(image)  # 画笔
    for x1, y1, x2, y2 in rects:  # 逐个遮罩
        draw.rectangle([x1, y1, x2, y2], fill="white")  # 白底覆盖
    image.save(target)  # 保存脱敏图


def redact_image(source: Path, target: Path, rects: list[tuple[int, int, int, int]]) -> None:
    """
    对截图按矩形涂黑
        :param source: 原图
        :param target: 输出图
        :param rects: 像素矩形
    """
    image = Image.open(source).convert("RGB")  # 打开
    draw = ImageDraw.Draw(image)  # 画笔
    for x1, y1, x2, y2 in rects:  # 逐个遮罩
        draw.rectangle([x1, y1, x2, y2], fill="black")  # 深色底覆盖
    image.save(target, quality=92)  # 保存


def extract(case: dict) -> dict:
    """
    跑一条真实识别
        :param case: 试点案例配置
        :return: 结果字典
    """
    source = RAW / case["source"]  # 原始件
    if case["redacted"]:
        target = ASSETS / case["redacted"]  # 脱敏件
        if source.suffix.lower() == ".pdf":
            redact_pdf(source, target, case["rects"])  # PDF 遮罩
        else:
            redact_image(source, target, case["rects"])  # 图片遮罩
        content = target.read_bytes()  # 用脱敏件
        content_type = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
    else:
        content = source.read_bytes()  # 无 PII 的原件
        content_type = case["content_type"]

    images = prepare_attachment(content, content_type)  # 预处理
    result = asyncio.run(DocumentAgent().extract(images, {
        "category": "",  # 识别不需要类别
        "description": "",  # 识别不需要说明
    }))  # 真实调用

    amount = f"{result.amount:.2f}" if result.amount is not None else None  # 金额
    doc_date = result.document_date.isoformat() if result.document_date else None  # 日期
    return {
        "case_id": case["case_id"],  # 案例编号
        "source": case["source"],  # 原始文件
        "redacted": case["redacted"],  # 脱敏件
        "type": result.document_type,  # 类型
        "type_ok": result.document_type == case["expect_type"],  # 类型是否命中
        "evidence": result.evidence_level,  # 证据强度
        "amount": amount,  # 金额
        "amount_ok": amount == case["expect_amount"],  # 金额是否命中
        "date": doc_date,  # 日期
        "date_ok": doc_date in (case.get("expect_dates") or [case["expect_date"]]),  # 日期是否命中（多段行程可接受多个）
        "counterparty": result.counterparty,  # 商户
        "party_ok": case["expect_party"] in (result.counterparty or ""),  # 商户关键词
        "identifier_tail": (result.identifier or "")[-4:],  # 凭证号后四位
        "confidence": result.confidence,  # 置信度
        "needs_manual_review": result.needs_manual_review,  # 是否建议人工
        "summary": result.summary,  # 摘要
        "model": result.model,  # 模型
        "latency_ms": result.latency_ms,  # 耗时
        "prompt_tokens": result.prompt_tokens,  # 输入 token
        "completion_tokens": result.completion_tokens,  # 输出 token
    }


def main(argv: list[str] | None = None) -> int:
    """
    顺序跑试点案例并写报告
        :param argv: 参数列表（None 取 sys.argv）
        :return: 退出码
    """
    import argparse

    parser = argparse.ArgumentParser(description="real_self_v1 凭证识别试点")
    parser.add_argument("--only", default="", help="只跑指定 case_id，逗号分隔；结果合并进已有报告")
    args = parser.parse_args(argv)
    only = {item.strip() for item in args.only.split(",") if item.strip()}  # 过滤集合

    ASSETS.mkdir(parents=True, exist_ok=True)  # 确保目录存在
    existing = {}  # 已有结果
    if OUT.exists():
        for item in json.loads(OUT.read_text(encoding="utf-8")).get("results", []):
            existing[item.get("case_id")] = item

    selected = [case for case in CASES if not only or case["case_id"] in only]  # 本次要跑的
    results = []  # 结果
    for case in selected:  # 逐条
        try:
            results.append(extract(case))  # 识别
            print(f"{case['case_id']} OK")
        except DocumentAgentError as exc:
            results.append({"case_id": case["case_id"], "error": exc.message, "code": exc.code})
            print(f"{case['case_id']} FAIL {exc.code} {exc.message}")

    for item in results:  # 合并进已有结果
        existing[item["case_id"]] = item
    ordered = [existing[case["case_id"]] for case in CASES if case["case_id"] in existing]  # 固定顺序

    payload = {"version": "real_self_v1", "mode": "ocr_pilot", "n": len(ordered), "results": ordered}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for item in results:
        if "error" in item:
            print(item["case_id"], "ERROR", item["code"])
            continue
        print(
            f"{item['case_id']} type={item['type']}({'OK' if item['type_ok'] else 'x'}) "
            f"amount={item['amount']}({'OK' if item['amount_ok'] else 'x'}) "
            f"date={item['date']}({'OK' if item['date_ok'] else 'x'}) "
            f"party={item['counterparty']}({'OK' if item['party_ok'] else 'x'}) "
            f"conf={item['confidence']} tokens={item['prompt_tokens']}+{item['completion_tokens']}"
        )
    print(f"report={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
