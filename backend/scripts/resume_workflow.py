"""
从 MySQL 检查点恢复一次 AI 预审工作流（Phase F 运维脚本）
    使用场景：
        - worker/进程在节点执行中途崩溃，workflow_runs 停留在 running/failed；
        - 检查点已落库，但业务没有自动续跑，需要人工触发恢复。
    用法（在 backend 目录下）：
        python scripts/resume_workflow.py --run-id 12
        python scripts/resume_workflow.py --run-id 12 --dry-run   # 只检查是否有检查点
    注意：
        - 恢复会继续调用真实 LLM，产生 API 费用；
        - thread_id 固定为 workflow_runs.id，必须与崩溃前一致；
        - 重复恢复是安全的：已完成的 run 会直接返回最终检查点结果。
"""
import argparse
import sys
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dependencies import SessionLocal                       # noqa: E402
from app.models import WorkflowRun                              # noqa: E402
from app.workflow.checkpoint import build_checkpointer          # noqa: E402
from app.workflow.service import resume_review_workflow         # noqa: E402



# 解析参数
def parse_args() -> argparse.Namespace:
    """
    解析命令行参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="从 MySQL 检查点恢复 AI 预审工作流")
    parser.add_argument("--run-id", type=int, required=True, help="workflow_runs.id")   # 运行ID
    parser.add_argument("--dry-run", action="store_true", help="只检查检查点，不执行恢复")   # 预演开关
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    脚本入口
        :return: 进程退出码：0 成功，1 失败
    """
    args = parse_args()
    db = SessionLocal()
    try:
        # 1.确认 run 存在
        run = db.query(WorkflowRun).filter(WorkflowRun.id == args.run_id).first()
        if run is None:
            print(f"工作流运行不存在: {args.run_id}")
            return 1

        # 2.检查是否有检查点
        checkpointer = build_checkpointer()
        config = {"configurable": {"thread_id": str(run.id)}}
        has_checkpoint = checkpointer.get_tuple(config) is not None
        print(f"run_id={run.id} status={run.status.value} checkpoint={has_checkpoint}")
        if args.dry_run:
            return 0 if has_checkpoint else 1
        if not has_checkpoint:
            print("没有可恢复的检查点")
            return 1

        # 3.执行恢复
        result = resume_review_workflow(db, run_id=run.id)
        print(
            f"恢复完成 run_id={result.run_id} "
            f"risk={result.outcome.risk_level} score={result.outcome.risk_score} "
            f"decision={result.decision.get('decision')}"
        )
        return 0
    except Exception as exc:
        print(f"恢复失败: {type(exc).__name__}: {exc}")
        return 1
    finally:
        db.close()



if __name__ == "__main__":
    raise SystemExit(main())
