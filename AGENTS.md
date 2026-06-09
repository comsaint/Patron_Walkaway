# AGENTS.md

## Cursor Cloud specific instructions

### 產品與此 checkout 範圍

本 repo 為 **Patron Walkaway**（賭場 Smart Table 離場預測 ML 系統）。此 checkout **主要含** `trainer_hightier/`、`baseline_models/`、契約腳本與文件；**不含** Phase 1 的 `trainer/`、`tests/`（LDA）、`package/` 等目錄（README 有提及但不在此樹中）。

可在此環境驗證的路徑：
- `trainer_hightier` 訓練／serving／deploy gate
- `make check-layered-contracts`（契約驗證；見下方注意）
- `make check-trainer-hightier-deploy-e2e-gate`（CI 同等測試）

### Python 環境

- **Python 3.12**（`python3`）；Makefile 使用 `python`，請用 **`.venv`**（`python3 -m venv .venv`）使 `python` 可用。
- 若 `python3 -m venv` 失敗，需一次性安裝：`sudo apt-get install -y python3.12-venv`。
- 啟用：`. .venv/bin/activate`
- 依賴安裝（與 `.github/workflows/trainer_hightier_deploy_e2e_gate.yml` 對齊）：

```bash
pip install ./trainer_hightier pytest pandas pyarrow scikit-learn pyyaml duckdb python-dotenv clickhouse-connect lightgbm feast "ibis-framework[duckdb]>=11.0.0,<12" jsonschema optuna
```

完整鎖檔見根目錄 `requirements.txt`（體積大；多數開發只需上列 CI 套件）。

### 常用指令

| 目的 | 指令 |
|------|------|
| 高階客群 precision floor 示範 | `python -m trainer_hightier` |
| ML API（Flask，預設 8001） | `python -m trainer_hightier.run_hightier_api --host 127.0.0.1 --port 8001` |
| Deploy E2E gate（CI） | `make check-trainer-hightier-deploy-e2e-gate` |
| 全量 hightier 測試 | `python -m pytest trainer_hightier/tests/ -q` |
| 分層契約驗證 | `make check-layered-contracts` |
| Lint（選用） | `ruff check trainer_hightier baseline_models scripts --exclude trainer_hightier/tests` |

### 服務啟動

- **ML API**：SQLite 狀態檔預設在 `trainer_hightier/local_state/`；無需 ClickHouse 即可回 `/health`、`/alerts`（空列表）。
- **ClickHouse**：生產路徑需要；憑證範本 `credential/.env.example`。本地開發可用 Parquet（`data/partitions/`）。
- **MLflow**：選用；未設定時訓練不中斷。

長時間執行請用 tmux（例如 API：`tmux new-session -d -s hightier-api` 後在 session 內啟動上述 API 指令）。

### 已知限制（此 checkout）

- `make check-lda-l0` 需要根目錄 `tests/unit/`（**不存在**於此 checkout）。
- `python -m baseline_models smoke` 需 `trainer` / `pip install -e .`（**trainer 目錄缺失**）。
- `make check-layered-contracts` 若報 `t_bet: missing keys: ['expected_delay_profile']`，為契約 registry 與驗證腳本不一致，非環境安裝問題。
- 部分 pytest 與主機時區或 MLflow 連線有關（例如 `test_session_preprocess.py`、`test_mlflow_adapter.py`）；CI gate 目標為 `test_deploy_e2e_gate.py`（16 項中可能有 1 項與 repo 根路徑解析相關）。
