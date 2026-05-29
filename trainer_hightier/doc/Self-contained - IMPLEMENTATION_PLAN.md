# trainer_hightier - Self-contained Implementation Plan

> Historical reference. The current scorer packaging/runtime source of truth is
> [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md).
> If this document conflicts with that SSOT, follow the SSOT.
> This plan predates scorer v2 Feast runtime adoption. Its `fe_short_term_parquet`, `mid_term_snapshot_parquet`, and
> `slow_patron_parquet` supplier rules are historical and must not be used to justify scorer v2 production fallback.

本文件屬於 **Implementation plan 層**，定義如何把 `trainer_hightier` 做成「**部署 runtime 自包含**」：打包後在 target 機只需 Python + 安裝 wheel/requirements + `.env`，即可啟動 scorer/api/validator。本文描述 realization strategy、模組邊界、里程碑、風險與驗證；不展開 ticket 級任務。

## 目標與邊界

- 目標：`trainer_hightier` **部署 runtime 路徑**（`serving/*`、`deploy/*`、打包輸出）不依賴 `trainer.*`。
- 目標：輸出可部署的 Runtime Bundle（入口、設定模板、模型與快照 artifacts、安裝契約）。
- 目標：target 機流程收斂為：`pip install`（PyPI + 本地 wheel）→ 設定 `.env` → `python main.py`。
- 目標：self-contained bundle 的 preflight 必須驗證模型欄位的 cadence-aware runtime suppliers；不能只驗 `model.pkl` / manifest 檔案存在。
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

- 產出 deploy bundle（`models/`, `snapshots/`, `mapping/`, `feast_repo/`, `artifacts/feast/`, `local_state/`）。
- 產出 runtime 契約檔（`deploy_bundle_paths.json`, `bundle_info.json`, `README_DEPLOY.md`）。
- 產出安裝契約（`requirements.txt`，可連 PyPI / internal index；`wheels/` 第一版只需 local package wheel，third-party wheelhouse 為備援）。
- strict 檢查 manifest/hash/path 完整性。
- 讀取 frozen registry + `model.pkl.feature_columns`，驗證每個欄位在 self-contained runtime 中有對應 supplier：
  - baseline raw 欄位由 ClickHouse query 供應。
  - `feast_trial_1h` 由 serving online PIT builder 供應；trial parquet 僅為診斷 artifact。
  - short-term `fe__*` 舊規劃可由 `fe_short_term_parquet` 或明確 online/micro-batch supplier 供應；scorer v2 改由 bounded on-the-fly PIT builder 供應目前模型欄位。
  - mid-term `fe__*` 舊規劃可由 production-scoped `mid_term_snapshot_parquet` 供應；scorer v2 改由 Feast online lookup 供應。
  - long-term `patron__*` 舊規劃可由 `slow_patron_parquet` 供應；scorer v2 改由 Feast online lookup 供應。

### 3) Runtime Entrypoint

- bundle 內存在 `main.py`（或等效入口）。
- 入口可直接啟動 scorer/api/validator（all/api/scorer/validator mode）。
- 啟動前檢查 `model.pkl`, `active_manifest.json`, required parquet, mapping。
- 啟動前 feature preflight 必須與 packaging 使用同一套 supplier contract，避免 API alive 但 scorer 第一輪缺欄。

### 4) Manifest Layer Contract

- `active_manifest.json` 是 bundle 內 snapshot layer SSOT。
- Required base layers（historical Parquet route）：`slow_patron_parquet`, `adt_allowlist_parquet`。
- Conditional feature layers：
  - `fe_short_term_parquet`：historical route only；scorer v2 production readiness 不接受此 layer。
  - `mid_term_snapshot_parquet`：historical route only；scorer v2 runtime supplier 是 Feast online lookup。
  - `fe_derived_parquet`：historical compatibility alias；不得作為 scorer v2 production readiness 或 fallback。
- Training-scoped mid-term artifacts 不得進入 production manifest；若 manifest 缺 production-safe mid-term snapshot，self-contained bundle 必須 fail-fast 或先透過 refresh/bootstrap 發佈 production snapshot。

### 5) Config Contract

- `.env.example` 定義必填設定（含 CH 連線）。
- `.env` 僅承載 secrets 與 operator 參數，禁止依賴 repo 本地路徑。
- Scorer v2 Feast path 必須 bundle-local：`feast_repo/`、`artifacts/feast/`、`local_state/feature_state.db`。
- Feast `feature_store.yaml`、registry、online store path 不可保留 dev-machine absolute path；deploy startup 必須 resolve / rewrite。

## Workstreams / Phases

### Phase 0：Runtime Baseline Freeze

- 凍結 runtime 驗收基準（bundle 結構、啟動命令、必要檢查欄位）。
- 建立 smoke checklist（無 repo 目標機）。

### Phase 1：Runtime Dependency Detach

- runtime 路徑去除 `trainer.*` import。
- 補齊內部 adapter（bundle paths / preflight / runtime helper）。

### Phase 2：Package And Bootstrap Contract

- 封裝 `trainer_hightier` runtime code 為 wheel 並納入部署流程。
- bundle 交付 `main.py`、`.env.example`、`requirements.txt`（PyPI / internal index + 本地 wheel）。
- bundle 交付 scorer v2 Feast runtime path contract（`feast_repo/`、`artifacts/feast/`、`local_state/feature_state.db`）。
- README/RUNBOOK 收斂為單一路徑啟動指令。
- bundle 交付 cadence-aware `active_manifest.json` layer contract，並在 build / boot preflight 驗證 feature supplier matrix。

### Phase 3：Deployment Validation

- 乾淨 target 機（無 repo）驗證安裝與啟動。
- 驗證 folder/zip 等價行為與回滾流程。

## 里程碑（Milestones）

- M1：runtime 路徑不再 import `trainer.*`。
- M2：bundle 具備完整啟動契約（`main.py`、`.env.example`、`requirements.txt`）。
- M3：無 repo 目標機可啟動 scorer/api/validator。
- M4：啟動與 runtime 可觀測 model/manifest/allowlist version/hash。
- M5：historical self-contained preflight 可攔截缺失或過期的 `fe_short_term_parquet` / `mid_term_snapshot_parquet`，不依賴 legacy `fe_derived_parquet`；scorer v2 以 Feast / bounded PIT readiness 為準。
- M6（future must-do）：post-startup scheduled/daemon Feast online refresh（startup auto-refresh 不足以支撐跨 gaming day 無重啟）。

## 風險與緩解

- 風險：PyPI 套件漂移造成安裝或行為不一致。
  - 緩解：鎖定版本；可選內部 mirror/wheels 備援。
- 風險：`active_manifest.json` 缺失導致建包失敗。
  - 緩解：前置檢查 + `--snapshot-manifest-source` 明確覆寫。
- 風險：bundle 缺入口/設定模板，無法在 target 機一鍵啟動。
  - 緩解：將 `main.py`/`.env.example` 納入必帶契約與 release gate。
- 風險：self-contained bundle 啟動成功，但模型欄位缺 cadence-aware supplier，第一輪 scoring 才失敗。
  - 緩解：build/deploy preflight 必須讀 frozen registry 與 manifest，按當前 SSOT 驗證 Feast / bounded PIT / raw supplier readiness；本文件中的 Parquet supplier matrix 僅為 historical route。
- 風險：為了通過 self-contained 打包而把 training-scoped mid-term snapshot 放進 production manifest。
  - 緩解：manifest layer contract 必須檢查 `snapshot_scope` / freshness / grain；unsafe artifact fail-fast，不做 silent fallback。

## 驗證與治理

### Validation

- runtime 單元測試：bundle path、preflight、入口參數、manifest 驗證。
- no-repo 整合測試：`pip install -r requirements.txt` 後直接啟動。
- feature supplier smoke：使用 frozen registry + model columns 驗證 short-term / mid-term / slow suppliers 完整；scorer v2 需驗證 bounded PIT + Feast readiness，並確認 legacy-only `fe_derived_parquet` 不會誤放行。
- 非功能：啟動延遲、記憶體峰值不超出基線。

### Governance

- runtime import guard：禁止 `deploy/*`、`serving/*`、`core/*` 引入 `trainer.*`。
- 文件治理：task breakdown 與執行細節僅維護在 Working Plan，不回灌本文件。
