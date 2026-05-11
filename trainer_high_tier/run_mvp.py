"""High-tier MVP 單一路徑骨架；各 step 僅列印／回傳佔位，方便之後接 trainer 或資料源。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HighTierMvpConfig:
    """之後可改為單一 yaml/py；先保留最少欄位避免依賴環境變數。"""

    data_dir: Path
    output_dir: Path


def ingest_high_tier(config: HighTierMvpConfig) -> Path:
    """讀取資料並篩成 high-tier 子集；回傳中間表路徑（佔位）。"""
    _ = config
    return Path("_mvp_ingest_placeholder.parquet")


def build_features(config: HighTierMvpConfig, ingest_path: Path) -> Path:
    """特徵 join / PIT 對齊（佔位）；ingest_path 為 ingest 產物。"""
    _ = (config, ingest_path)
    return Path("_mvp_features_placeholder.parquet")


def time_split_train_valid(
    config: HighTierMvpConfig, feature_path: Path
) -> tuple[Path, Path]:
    """時間切分 train / valid（佔位）；回傳兩個 split 路徑。"""
    _ = (config, feature_path)
    return Path("_mvp_train.parquet"), Path("_mvp_valid.parquet")


def train_model(
    config: HighTierMvpConfig, train_path: Path, valid_path: Path
) -> Path:
    """擬合模型並寫 checkpoint（佔位）。"""
    _ = (config, train_path, valid_path)
    return Path("_mvp_model_placeholder.pkl")


def write_serving_artifact(
    config: HighTierMvpConfig, model_path: Path
) -> Path:
    """匯出與線上 scorer 相容的產物 + manifest（佔位）。"""
    _ = (config, model_path)
    return config.output_dir / "_latest_serving_placeholder.json"


def run_end_to_end(config: HighTierMvpConfig) -> Path:
    """串起 end-to-end；目前不讀寫真實大檔，只跑通呼叫順序。"""
    config.output_dir.mkdir(parents=True, exist_ok=True)
    ingest_path = ingest_high_tier(config)
    feat_path = build_features(config, ingest_path)
    train_p, valid_p = time_split_train_valid(config, feat_path)
    model_p = train_model(config, train_p, valid_p)
    return write_serving_artifact(config, model_p)


def _parse_args(argv: list[str] | None = None) -> HighTierMvpConfig:
    """解析 CLI；預設路徑可之後對齊 repo 慣例。"""
    p = argparse.ArgumentParser(description="High-tier MVP pipeline (skeleton).")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="輸入資料根目錄（佔位）",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out") / "models_high_tier_mvp",
        help="模型與 serving 產物目錄",
    )
    ns = p.parse_args(argv)
    return HighTierMvpConfig(data_dir=ns.data_dir, output_dir=ns.output_dir)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    cfg = _parse_args(argv)
    out = run_end_to_end(cfg)
    print(f"[trainer_high_tier] skeleton done. serving placeholder: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
