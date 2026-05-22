# Production-like Deploy E2E Gate - IMPLEMENTATION_PLAN

本文件是 implementation plan layer，定義如何實作一個 production-like 的部署端到端驗證腳本，
用來覆蓋 deploy 啟動流程（refresh/apply/materialize/readiness/smoke/scorer），並以
`local_cleaned` 資料來源進行壓測。

## Objective

建立一個可重複執行的 release gate，最大化重現實際部署啟動路徑，避免「測試綠燈、上線紅燈」。

此 gate 要能在新 bundle（含空 Feast runtime 狀態）下，驗證：

- startup refresh 決策是否正確；
- Feast registry/apply/materialize 順序是否正確；
- readiness 發布與 smoke gate 是否符合 deploy 規則；
- scorer 在同一個 bundle 上是否可正常進入可評分狀態。

## Scope

Included:

- 以 deploy bundle 為輸入，走 production deploy 入口與 startup orchestration。
- 使用 `local_cleaned` 作為 refresh source（cleaned bet/session）。
- 覆蓋 slow-only 與 mid+slow 模型路徑（由 model supplier plan 自動判定）。
- 產出可機器讀取的執行報告（JSON）與失敗診斷資訊。

Excluded:

- 不覆蓋 ClickHouse raw 匯出 SQL 與資料庫連線層。
- 不覆蓋長時間服務進程壽命（daemon 級別）行為。
- 不重寫既有 feature 計算邏輯；僅編排並呼叫既有模組。

## Key Decisions

- Gate 定位為 production-like deploy startup 驗證，不是一般 parity 報表工具。
- 僅新增「薄 orchestration 腳本」，不複製 `deploy.main` 或 `feast_online_refresh` 業務邏輯。
- 執行失敗採 fail-fast；任何硬性 gate 失敗即非零結束碼。
- 強制使用 bundle-local 路徑與執行環境，避免全域 site-packages 汙染測試結論。
- 報告先 JSON-first，方便 CI 與 release pipeline 消費。

## Solution Architecture

### 1) Orchestration Entry

新增一個 deployment E2E gate 入口（單一腳本），負責：

- 載入 bundle 路徑與模型供應計畫；
- 驗證必要檔案契約（`deploy_bundle_paths.json`、mapping、allowlist、feast_repo）；
- 啟動 production-like startup 驗證流程；
- 彙整結果並輸出報告。

### 2) Startup Simulation Path

E2E gate 的核心流程：

1. 判定模型是否需要 mid/slow Feast suppliers。
2. 觸發 startup refresh（source 設定為 `local_cleaned`）。
3. 驗證 registry 缺失情境下，`feast apply` 必定先於 materialize。
4. 驗證 readiness 檔與 smoke gate 通過後才視為 startup 成功。

### 3) Scorer Readiness Confirmation

startup 成功後，執行最小 scorability 驗證：

- 使用同 bundle 的 scorer feature 路徑完成一次小樣本評分準備；
- 驗證無硬性 readiness/smoke 失敗；
- 記錄 entity missing、cell null、post-join smoke 指標。

### 4) Evidence and Reporting

每次執行輸出 JSON 報告，至少包含：

- bundle 與模型版本資訊；
- startup 各步驟狀態（refresh/apply/materialize/readiness/smoke）；
- gate verdict 與 failure reason；
- 重要 artifact 路徑（readiness、refresh summary、state db、feast repo）。

## Module Boundaries

- `deploy.main`、`feast_online_refresh`、`feast_readiness`：維持既有職責，不在新腳本中複製邏輯。
- 新 E2E gate 腳本：只負責流程編排、輸入組態、結果整合。
- `offline_serving_backtest`：保留既有 replay 定位；不作為 startup orchestration 的唯一驗證工具。

## Phases and Milestones

### Phase 1: Minimal Viable Gate

- 建立新 E2E gate 入口與 CLI。
- 打通 local_cleaned 的 startup-like refresh 路徑。
- 針對「空 registry 的新 bundle」建立必測情境。

Milestone:

- 能穩定重現並阻擋「未 apply 先 materialize」類問題。

### Phase 2: Production-like Hardening

- 補齊 slow-only / mid+slow 分支判定與覆蓋。
- 報告欄位標準化並可供 CI 判斷。
- 增加 scorer 最小可評分確認。

Milestone:

- 可作為 release 前必跑 gate，且結果可追溯。

### Phase 3: CI / Release Integration

- 將 E2E gate 接入 release pipeline。
- 設定 fail-fast 規則與 artifact 留存策略。

Milestone:

- bundle promotion 需通過此 gate 才可進入下一階段。

## Risks and Mitigations

- 與 production 行為漂移：
  - 緩解：只調用 production 模組，不複製核心邏輯。
- local_cleaned 與 raw source 差異：
  - 緩解：文件中明確標示 coverage 邊界，並保留後續 raw-source gate 擴充位。
- 環境汙染導致假陰性/假陽性：
  - 緩解：強制輸出執行中的 `trainer_hightier` 載入路徑與 bundle runtime 路徑。

## Validation Strategy

驗證分三層：

- 單元：新腳本的輸入解析、路徑契約、報告格式。
- 整合：mock 最小資料下的 startup gate 分支（registry 缺失、slow-only、mid+slow）。
- 壓測：在實際 dev bundle + local_cleaned 資料上跑完整流程，作為 release gate。

## Rollout and Governance

- 初期以 warning + 報告模式觀察一個 migration 週期。
- 穩定後升級為 hard gate（非零即阻擋 promotion）。
- 每次 incident 需回寫此 gate 是否可提前攔截，作為治理回饋。

## CLI and CI

Entry point (defaults match production deploy: new ``.venv``, ``pip install -r requirements.txt``, cold Feast runtime):

```bash
python -m trainer_hightier.serving.deploy_e2e_gate \\
  --bundle-dir /path/to/deploy_bundle \\
  --local-cleaned-bet /path/to/cleaned__gmwds_t_bet \\
  --local-cleaned-session /path/to/cleaned__gmwds_t_session.parquet \\
  --output-json artifacts/feast/deploy_e2e_gate_report.json
```

The driver re-execs itself under ``bundle/.venv/Scripts/python.exe`` (Windows) after provisioning.

Flags:

- ``--no-provision-venv`` — skip venv create/install (only when already inside bundle venv)
- ``--reuse-venv`` — keep existing ``.venv`` instead of deleting it first
- ``--no-reset-feast-runtime`` — keep ``registry.db`` / ``feast_online_readiness.json``
- ``--warn-only`` — failures emit ``verdict=warn`` but exit 0 (CI migration)

Report schema: ``deploy_e2e_gate_v1`` (see ``trainer_hightier/serving/deploy_e2e_gate.py``).

CI (orchestration regression tests, no large data):

```bash
make check-trainer-hightier-deploy-e2e-gate
```
