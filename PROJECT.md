# Patron Walkaway — 專案結構與目錄職責

本文件為專案整體結構的 **單一事實來源（SSOT）**，與 `.cursor/plans/PLAN.md` § Phase 2 前結構整理對齊。後續目錄搬移、產出路徑、建包設定均以此為準。

---

## 目標目錄樹（對照用）

以下為 Phase 2 前結構整理完成後的目標結構；目前部分產出仍寫入 `trainer/models/`、`trainer/out_backtest/`，將於計畫「項目 4」改為約定路徑。

```
Patron_Walkaway/
├── PROJECT.md              # 本文件：專案結構 SSOT
├── README.md
├── pyproject.toml, setup.py, requirements.txt, ruff.toml
├── data/                   # 輸入資料、共用資料（Parquet、canonical_mapping、player_profile）
│   └── (out/ 為可選：若採用 data/out/ 則訓練/回測產出放此；目前約定為根目錄 out/)
├── out/                    # 產出約定：訓練/回測/model 產出（見下方「產出與可執行腳本」）
│   ├── models/
│   ├── backtest/
│   └── ...
├── doc/
│   └── one_time_scripts/   # 歷史／一次性腳本（僅供參考、勿直接執行）
├── schema/, ssot/, .cursor/plans/
├── scripts/                # 可重複執行腳本（含 check_span.py）
├── package/
├── tests/
└── trainer/
    ├── __init__.py
    ├── core/               # 項目 2 後：config, db_conn, schema_io, duckdb_schema
    ├── features/           # 項目 2 後：features 相關 + feature_spec/
    ├── training/           # 項目 2 後：trainer, time_fold, backtester
    ├── serving/            # 項目 2 後：scorer, validator, api_server, status_server
    ├── etl/                # 項目 2 後：etl_player_profile, profile_schedule
    ├── scripts/            # 既有 trainer/scripts（analyze, auto_build, recommend_config, ...）
    └── frontend/           # 儀表板 SPA（可選；部署可僅含 API）
```

**產出路徑約定**：採用根目錄 **`out/`**（不採用 `data/out/`）。預設 model 目錄為 `out/models/`，backtest 輸出為 `out/backtest/`。在「項目 4」實施前，程式仍寫入 `trainer/models/`、`trainer/out_backtest/`。

---

## 各頂層目錄職責

| 目錄 | 職責 |
|------|------|
| **data/** | 輸入與共用資料：本地 Parquet（`gmwds_t_bet.parquet`、`gmwds_t_session.parquet`）、`player_profile.parquet`、`canonical_mapping.parquet` 與 sidecar。可選 `.gitignore` 避免大檔進版控。 |
| **out/** | 訓練與回測產出（項目 4 實施後）：`out/models/`（model.pkl、feature_list.json、feature_spec.yaml 等）、`out/backtest/`（回測報表與預測）。 |
| **doc/** | 規格、發現、API 協定、一次性／歷史腳本（`one_time_scripts/`，僅供參考、勿直接執行）。 |
| **schema/** | 資料表／欄位字典與 DQ 提示。 |
| **ssot/** | 訓練／標籤／特徵設計規格（單一事實來源）。 |
| **.cursor/plans/** | 實作計畫（PLAN.md）、狀態（STATUS.md）、決策紀錄（DECISION_LOG.md）、部署計畫（DEPLOY_PLAN.md）。 |
| **scripts/** | 可重複執行腳本（含 `check_span.py`）。 |
| **package/** | 建包與部署：`build_deploy_package.py`、`deploy/`。 |
| **tests/** | 單元與整合測試（pytest）；結構暫不變，或後續再分 unit/integration/review_risks。 |
| **trainer/** | 核心程式：config、trainer、features、scorer、validator、ETL 等；項目 2 後拆為 core/features/training/serving/etl 子包。 |

---

## 重要入口

| 用途 | 指令或入口 |
|------|------------|
| **訓練** | `python -m trainer.trainer`（可加 `--use-local-parquet`、`--recent-chunks N`、`--skip-optuna` 等） |
| **回測** | `python -m trainer.backtester --start YYYY-MM-DD --end YYYY-MM-DD`（可加 `--use-local-parquet`、`--skip-optuna`） |
| **即時打分** | `python -m trainer.scorer --interval 45 --lookback-hours 8`（單次加 `--once`） |
| **驗證** | `python -m trainer.validator --interval 60`（單次加 `--once`） |
| **API** | `python -m trainer.api_server`（預設 http://0.0.0.0:8001） |
| **Status server** | `python -m trainer.status_server` |
| **建包** | `python -m package.build_deploy_package`（產出 `deploy_dist/`，可加 `--archive`） |
| **Profile ETL** | `python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` |

**生產環境**：API 與 Status server 於生產環境請以 WSGI server（如 gunicorn）掛載 `trainer.serving.api_server:app`，勿以 `python -m trainer.api_server` 直接對外服務。

---

## 文件索引

| 位置 | 用途與主要檔案 |
|------|----------------|
| **.cursor/plans/** | **詳細實作計畫與狀態**：`PLAN.md`（Phase 1 與 Phase 2 前結構整理）、`STATUS.md`（回合摘要與驗證）、`DECISION_LOG.md`（架構決策）、`DEPLOY_PLAN.md`（部署步驟）。規格與 Phase 2 延伸文件在 `doc/`。 |
| **doc/** | 規格與說明：`FINDINGS.md`、`player_profile_spec.md`、`FEATURE_SPEC_GUIDE.md`、`model_api_protocol.md`、`TRAINER_SUMMARY.md` 等；`one_time_scripts/` 為歷史／一次性腳本（僅供參考、勿直接執行）。 |
| **schema/** | 資料表／欄位字典與 DQ 備註（如 `GDP_GMWDS_Raw_Schema_Dictionary.md`）。 |
| **ssot/** | 訓練／標籤／特徵設計規格（如 `trainer_plan_ssot.md`）。 |
| **package/** | 部署用說明與協定：`README.md`、`ML_API_PROTOCOL.md`。 |

---

## 產出與可執行腳本約定

- **產出**：統一放到根目錄 **`out/`**（不採用 `data/out/`）。預設 model 目錄為 `out/models/`，backtest 目錄為 `out/backtest/`。在 config 與建包改為讀取此約定前，現有程式仍寫入 `trainer/models/`、`trainer/out_backtest/`。實施項目 4 時應將 `out/` 加入 `.gitignore`，避免產出進入版控。
- **可重複執行腳本**：放在 **`scripts/`**（含 `check_span.py`）。
- **一次性／歷史腳本**：放在 **`doc/one_time_scripts/`**；僅供參考，勿直接執行。

---

## 前端與部署（項目 8）

- **trainer/frontend/** 為儀表板 SPA，**可選**；部署包可僅含 API（無前端），若需儀表板再一併打包。
- 預設建包是否含 frontend、靜態檔位置等見 `package/README.md` 或 deploy 說明。
- （可選）若未來前端擴充（例如監控 UI），可考慮將 `trainer/frontend/` 提到根目錄 `frontend/`，建包時再產出到 deploy 目錄。

---

*改動目錄或新增模組時，請同步更新本文件（PROJECT.md）以維持結構 SSOT。*
