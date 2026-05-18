# trainer_hightier - Self-contained Implementation Plan

本文件屬於 **Implementation plan 層**，定義如何把 `trainer_hightier` 做成「**部署 runtime 自包含**」：打包後在 target 機只需 Python + 安裝 wheel/requirements + `.env`，即可啟動 scorer/api/validator。本文描述 realization strategy、模組邊界、里程碑、風險與驗證；不展開 ticket 級任務。

## 目標與邊界

- 目標：`trainer_hightier` **部署 runtime 路徑**（`serving/*`、`deploy/*`、打包輸出）不依賴 `trainer.*`。
- 目標：輸出可部署的 Runtime Bundle（入口、設定模板、模型與快照 artifacts、安裝契約）。
- 目標：target 機流程收斂為：`pip install`（PyPI + 本地 wheel）→ 設定 `.env` → `python main.py`。
- 非範圍：訓練/特徵實驗路徑完全去除第三方專案依賴（例如 `pipelines.layered_data_assets`）。

## 依賴策略（只針對 runtime）

### A. Runtime 必拆依賴（硬性）

- `trainer.core.model_bundle_paths`
- `trainer.core.mlflow_utils`（若 runtime 仍碰到）
- `trainer.training.cross_entry_preflight`
- 任何 runtime import `trainer.*`

### B. Training/Preprocess 依賴（可保留）

- `pipelines.layered_data_assets.*` 可保留於訓練流程，不作為 runtime go-live blocker。

### C. 測試依賴

- runtime 相關測試不得依賴 `trainer.*`。
- 允許保留少量 training parity 測試於非 runtime 測試群組。

## 目標架構與模組邊界

### 1) Runtime Package Boundary

- runtime 模組（`deploy/*`, `serving/*`, `core/*`）僅依賴 `trainer_hightier` 內部 API。
- 對外系統依賴僅限 Python 套件（由 wheel + requirements 管理）。

### 2) Packaging Compiler（`build_deploy_package.py`）

- 產出 deploy bundle（`models/`, `snapshots/`, `mapping/`, `local_state/`）。
- 產出 runtime 契約檔（`deploy_bundle_paths.json`, `bundle_info.json`, `README_DEPLOY.md`）。
- 產出安裝契約（`requirements.txt`，可連 PyPI；可選 `wheels/` 備援）。
- strict 檢查 manifest/hash/path 完整性。

### 3) Runtime Entrypoint

- bundle 內存在 `main.py`（或等效入口）。
- 入口可直接啟動 scorer/api/validator（all/api/scorer/validator mode）。
- 啟動前檢查 `model.pkl`, `active_manifest.json`, required parquet, mapping。

### 4) Config Contract

- `.env.example` 定義必填設定（含 CH 連線）。
- `.env` 僅承載 secrets 與 operator 參數，禁止依賴 repo 本地路徑。

## Workstreams / Phases

### Phase 0：Runtime Baseline Freeze

- 凍結 runtime 驗收基準（bundle 結構、啟動命令、必要檢查欄位）。
- 建立 smoke checklist（無 repo 目標機）。

### Phase 1：Runtime Dependency Detach

- runtime 路徑去除 `trainer.*` import。
- 補齊內部 adapter（bundle paths / preflight / runtime helper）。

### Phase 2：Package And Bootstrap Contract

- 封裝 `trainer_hightier` runtime code 為 wheel 並納入部署流程。
- bundle 交付 `main.py`、`.env.example`、`requirements.txt`（PyPI + 本地 wheel）。
- README/RUNBOOK 收斂為單一路徑啟動指令。

### Phase 3：Deployment Validation

- 乾淨 target 機（無 repo）驗證安裝與啟動。
- 驗證 folder/zip 等價行為與回滾流程。

## 里程碑（Milestones）

- M1：runtime 路徑不再 import `trainer.*`。
- M2：bundle 具備完整啟動契約（`main.py`、`.env.example`、`requirements.txt`）。
- M3：無 repo 目標機可啟動 scorer/api/validator。
- M4：啟動與 runtime 可觀測 model/manifest/allowlist version/hash。

## 風險與緩解

- 風險：PyPI 套件漂移造成安裝或行為不一致。
  - 緩解：鎖定版本；可選內部 mirror/wheels 備援。
- 風險：`active_manifest.json` 缺失導致建包失敗。
  - 緩解：前置檢查 + `--snapshot-manifest-source` 明確覆寫。
- 風險：bundle 缺入口/設定模板，無法在 target 機一鍵啟動。
  - 緩解：將 `main.py`/`.env.example` 納入必帶契約與 release gate。

## 驗證與治理

### Validation

- runtime 單元測試：bundle path、preflight、入口參數、manifest 驗證。
- no-repo 整合測試：`pip install -r requirements.txt` 後直接啟動。
- 非功能：啟動延遲、記憶體峰值不超出基線。

### Governance

- runtime import guard：禁止 `deploy/*`、`serving/*`、`core/*` 引入 `trainer.*`。
- 文件治理：task breakdown 與執行細節僅維護在 Working Plan，不回灌本文件。
