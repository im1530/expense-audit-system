"""
运维告警巡检脚本（P1：脚本 + 日志实现，不接第三方告警平台）
    检查项：
        - maintenance_runs 失败 / 卡在 running；
        - workflow_runs 僵尸（长时间 running）；
        - ai_review_logs 僵尸（长时间 pending/running）；
        - 当日 token / 费用 / 调用次数是否触及预算；
        - 可选：对 /health 探活（传 --base-url 才做）。
    用法（在 backend 目录下）：
        python scripts/check_alerts.py                       # 人类可读输出
        python scripts/check_alerts.py --json                # 机器可读，便于接监控平台
        python scripts/check_alerts.py --base-url http://127.0.0.1:8000 --state-file logs/alert_state.json
    退出码：
        0 无告警（或都被抑制）
        1 只有 warning
        2 存在 critical
    建议：
        - 用 Windows 任务计划 / cron 每 10~15 分钟执行一次；
        - 配 --state-file 做抑制，避免同一问题每 15 分钟刷一次日志；
        - 输出同时写 --log-file，便于事后追溯。
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                                         # noqa: E402
from app.dependencies import SessionLocal                               # noqa: E402
from app.services.alert_service import (                                # noqa: E402
    SEVERITY_CRITICAL,
    collect_alerts,
    filter_suppressed,
)



logger = logging.getLogger("check_alerts")   # 模块日志器



# 读取抑制状态
def load_state(path: Path | None) -> dict:
    """
    读取上次告警状态
        :param path: 状态文件路径（None 表示不抑制）
        :return: 状态字典；文件缺失或损坏时返回空字典
    """
    if path is None or not path.exists():                           # 未配置或不存在
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))         # 读 JSON
    except (OSError, json.JSONDecodeError):                         # 损坏则当作无状态
        logger.warning("告警状态文件无法解析，按无状态处理: %s", path)
        return {}



# 保存抑制状态
def save_state(path: Path | None, state: dict) -> None:
    """
    保存告警状态
        :param path: 状态文件路径
        :param state: 状态字典
        :return: 无
    """
    if path is None:                                                # 未配置则不落盘
        return
    path.parent.mkdir(parents=True, exist_ok=True)                  # 确保目录存在
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")   # 写 JSON



# 解析参数
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数
        :param argv: 参数列表（测试可注入）
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="运维告警巡检")
    parser.add_argument("--base-url", default=None, help="传入则对 <base-url>/health 探活")
    parser.add_argument("--lookback-hours", type=int, default=24, help="运维失败回看窗口（小时）")
    parser.add_argument("--maintenance-stuck-minutes", type=int, default=60, help="运维任务卡死阈值（分钟）")
    parser.add_argument("--workflow-stale-minutes", type=int, default=0, help="工作流超时阈值；0 读配置")
    parser.add_argument("--review-stale-minutes", type=int, default=0, help="预审超时阈值；0 读配置")
    parser.add_argument("--state-file", default=None, help="抑制状态文件（JSON）；不传则不抑制")
    parser.add_argument("--suppress-minutes", type=int, default=60, help="同一告警抑制窗口（分钟）")
    parser.add_argument("--log-file", default=None, help="把结果同时追加写入该文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser.parse_args(argv)



# 主流程
def main(argv: list[str] | None = None) -> int:
    """
    执行一次巡检
        :param argv: 命令行参数
        :return: 退出码：0 无告警 / 1 warning / 2 critical
    """
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    state_path = Path(args.state_file) if args.state_file else None  # 抑制状态文件
    now = datetime.now()                                             # 当前时间
    db = SessionLocal()                                              # 数据库会话
    try:
        # 1.收集全部告警
        alerts = collect_alerts(
            db,
            now=now,
            base_url=args.base_url,
            lookback_hours=args.lookback_hours,
            maintenance_stuck_minutes=args.maintenance_stuck_minutes,
            workflow_stale_minutes=args.workflow_stale_minutes or None,
            review_stale_minutes=args.review_stale_minutes or None,
        )
    finally:
        db.close()                                                   # 关闭会话

    # 2.抑制过滤
    state = load_state(state_path)                                   # 上次状态
    emitted, new_state = filter_suppressed(
        alerts, state, now=now, suppress_minutes=args.suppress_minutes
    )
    save_state(state_path, new_state)                                # 落盘新状态

    # 3.输出
    critical = [alert for alert in emitted if alert.severity == SEVERITY_CRITICAL]   # 严重告警
    if args.json:                                                    # 机器可读
        print(json.dumps({
            "checked_at": now.isoformat(timespec="seconds"),         # 巡检时间
            "total": len(alerts),                                    # 检出总数
            "emitted": len(emitted),                                 # 实际发出（未被抑制）
            "alerts": [alert.as_dict() for alert in emitted],        # 告警明细
        }, ensure_ascii=False, indent=2))
    else:                                                            # 人类可读
        print(f"[{now:%Y-%m-%d %H:%M:%S}] 巡检完成 检出={len(alerts)} 发出={len(emitted)}")
        for alert in emitted:
            print(f"  [{alert.severity.upper()}] {alert.code} | {alert.title} | {alert.detail}")
        if not emitted:
            print("  一切正常")

    # 4.可选：同时写日志文件
    if args.log_file:
        log_path = Path(args.log_file)                               # 日志路径
        log_path.parent.mkdir(parents=True, exist_ok=True)           # 确保目录存在
        with log_path.open("a", encoding="utf-8") as handle:         # 追加写
            stamp = now.strftime("%Y-%m-%d %H:%M:%S")                # 时间戳
            if not emitted:
                handle.write(f"{stamp} OK total={len(alerts)}\n")
            for alert in emitted:
                handle.write(f"{stamp} {alert.severity.upper()} {alert.code} {alert.detail}\n")

    # 5.退出码：给监控/计划任务判断
    if critical:
        return 2
    if emitted:
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
