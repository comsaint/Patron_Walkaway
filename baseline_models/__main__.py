"""``python -m baseline_models`` 入口。"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """CLI：``smoke``／``run`` 寫入 ``results/<run_id>/`` 三件套（實作相同）。

    Args:
        argv: 若 ``None`` 則使用 ``sys.argv[1:]``。

    Returns:
        處理結果之 process exit code。
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m baseline_models")
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("smoke", help="Phase A：契約檢查用最小樣本（常搭配 synthetic_smoke）")
    p_smoke.add_argument("--config", required=True, help="YAML 設定檔路徑")
    p_smoke.add_argument("--run-id", required=True, help="結果子目錄名，例如 20260418_baseline_smoke")

    p_run = sub.add_parser(
        "run",
        help="完整 baseline 評估（Tier-0／Tier-1 + 可選同窗）；與 smoke 為同一程式路徑",
    )
    p_run.add_argument("--config", required=True, help="YAML 設定檔路徑")
    p_run.add_argument("--run-id", required=True, help="結果子目錄名，例如 20260418_baseline_full")

    ns = parser.parse_args(args_list)
    if ns.command in ("smoke", "run"):
        from baseline_models.src.eval.runner import run_smoke

        print(
            f"[baseline_models] CLI command={ns.command!r} run_id={ns.run_id!r}",
            flush=True,
        )
        run_smoke(ns.config, ns.run_id)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
