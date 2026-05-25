# Scorer 執行期契約 — SSOT

本文件為 `trainer_hightier` scorer 建包與執行期就緒的**現行治理真相（SSOT）**。  
舊版 packaging、self-contained、mid-term snapshot 與 registry 實作計畫僅作歷史參考；若與本契約衝突，**以本文件為準**。

**決策紀錄（2026-05）**：未來 high-tier 模型採 **Option B — bounded ASOF（N=30）**；歷史 unlimited-ASOF 模型見 §「Option A（僅歷史模型）」。

---

## 相容性政策

Scorer v2 不再保留不必要的 legacy feature supplier 相容路徑。刻意保留的僅有營運與下游所需之外部契約：

- 維持 scorer 進入點與支援的 CLI 形狀，除非另有核准的替代方案。
- 維持對外 `state.db` 告警／驗證 schema 相容。
- **不得**在 production 執行期 fallback 至 legacy Parquet supplier。
- 測試可用 mock／fixture 注入；legacy Parquet **不得**進入 production scorer 主路徑。

---

## 目標

從可能沒有最新 production 資料的開發環境建置可部署 scorer bundle。Production 就緒性在 **deploy／scoring 當下**（可連 ClickHouse 或 production source mirror）驗證。

---

## 建包期契約（Package-Time）

`build_deploy_package` 產出 **Feast-only production bundle**；snapshot 特徵 Parquet **不**入包；mid／long 由 deploy 時 Feast online refresh 供應。

建包必須驗證 bundle 結構可用：

- model、frozen registry、wheel、mapping、allowlist、metadata-only manifest 可讀。
- `requirements.txt` 可從 PyPI 或內部 index 安裝。
- 依 frozen registry 將 model 欄位分類為已知 supplier（Feast／PIT／baseline）。
- 不得接受 training-scoped artifact 為 production-safe snapshot。
- `snapshots/active_manifest.json` 僅 **metadata**（版本、coverage、training cutoff、allowlist 稽核）；**不得**含 `*_parquet` 路徑鍵。
- 缺 legacy snapshot 路徑（`slow_patron_parquet`、`mid_term_snapshot_parquet` 等）**不**阻擋建包。
- `fe_short_term_parquet` 不入包、非 scorer v2 supplier。

目錄：`models/`、`mapping/`（含 `adt_allowed_players_q0p99.parquet`）、`feast_repo/`、`artifacts/feast/`、`local_state/`、metadata-only `snapshots/active_manifest.json`。

**含 mid-term 的未來模型（Option B）建包額外要求**：

- Step 6 產出 `feature_parity_verification.json`，且 **`hard_fail_all_feature_gate=True`**（含 mid 與 audit 欄位）。
- strict 模式下不得 `--skip-step6-gate`。

建包產物可以是「需 refresh 的 bundle」。

---

## 部署期契約（Deploy-Time）

Production deploy（`trainer_hightier.deploy.main`）在 scorer-capable 模式（`all`、`scorer`）負責 Feast 就緒：

- 載入 model、frozen registry、mapping、ADT allowlist、bundle-local Feast repo。
- 驗證 ClickHouse 憑證（bundle `.env`／環境覆寫）。
- Feast 路徑須 bundle-local，不得殘留 dev 機絕對路徑。
- startup 時若 readiness 缺失／過期／`--force-feast-refresh`，執行 **Feast online refresh**。
- bundle-local refresh lock；短逾時後 fail-fast。
- 僅依 config 與 readiness metadata 評估 mid／slow freshness。
- refresh 後將 readiness 寫入 `feature_state.db` 並發布 `feast_online_readiness.json`。
- deploy readiness gate + allowlist online lookup smoke；失敗則 **不得**啟動 API／validator／scorer。
- 模型含 short-term `fe__*` 時，bounded on-the-fly PIT 須支援全部所需欄位。
- **不得**以 legacy Parquet snapshot refresh 作為 scorer v2 mid／long 主路徑。
- 在 hard cap 內的 stale 可 **degraded scoring**，並寫入 prediction log。

`mode=api`、`mode=validator` 單獨啟動前，須 scorer-capable startup 已成功。

**Post-startup refresh（已採用）**：`mode=all`／`mode=scorer` 預設啟動 Feast refresh supervisor（`--no-feast-refresh-supervisor` 可關）。見 [`Feast Post-Startup Refresh Supervisor - IMPLEMENTATION_PLAN.md`](Feast%20Post-Startup%20Refresh%20Supervisor%20-%20IMPLEMENTATION_PLAN.md)。

**Option B 首次 deploy／bootstrap**：

- mid layer：`--bootstrap-mid`；seed **僅涵蓋 N 個 gaming day 的 anchor 歷史**（治理值 N=30，見 §設定 SSOT），不得將 unlimited 歷史 carry-forward 寫入 online store。
- 日常 incremental：`--skip-apply`；merge 路徑，避免 reset 後只 materialize 單日導致覆蓋崩塌。

---

## 打分期契約（Scoring-Time）

Scorer **不得**靜默補齊缺失的 model 特徵：

- `model.pkl.feature_columns` 每一欄在 `predict_proba` 前須存在（允許 **cell null**，見下）。
- 所需 feature family 在 join 後不得「全系統性全 null」而未記錄。
- 錯誤 grain 或 training-scoped mid snapshot 為 hard failure。
- 未實作的 short-term `fe__*` 為 hard failure。
- prediction log 須暴露 snapshot freshness／degraded 與 missing 計數。

**Mid-term 觀測（P1）**：`prediction_log` 記錄 `mid_term_anchor_gaming_day_max`、`mid_term_snapshot_age_days`、`mid_null_top_features_json`；`feast_online_readiness.json` mid 層記錄 `anchor_gaming_day_max`、`expected_anchor_gaming_day`、`snapshot_age_days`、smoke 的 `cell_null_counts`。

---

## Mid-term train／serve 契約

### Option B — 未來模型（治理預設）

適用於 **重新訓練並 promotion** 的 high-tier 模型；registry baseline 含 mid-term `fe__*` 與 audit 欄位。

#### 設定 SSOT（`trainer_hightier/config.py`）

| 常數／概念 | 治理值 | 說明 |
|-----------|--------|------|
| **N**（bounded ASOF 窗寬） | **30** gaming days | 訓練與 serving 共用；實作落地時建議命名如 `production_mid_asof_backfill_days` |
| `MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS` | **32** | materialize 用 bet 回看（w30d + buffer）；須 **≥ N** |
| `PRODUCTION_MID_FEAST_BOOTSTRAP_ANCHOR_DAYS` | **30**（對齊 N） | 首次 bootstrap materialize 的 anchor 天數上限 |
| `MID_TERM_STALE_HARD_CAP_DAYS` | **3** | **營運層**：全庫 `anchor_max` 相對預期 D−1 的 refresh SLA；**與 N 分離** |
| `SCORER_FEAST_MID_CELL_NULL_FAIL_FRACTION` | **0.05** | deploy／refresh smoke |
| `SCORER_FEAST_MID_MIN_CANONICAL_COVERAGE_FRACTION` | **0.95** | allowlist canonical 覆蓋 |

#### Anchor 有效區間（訓練與線上須一致）

對每一筆 bet，令 `G = gaming_day`，`A = anchor_gaming_day`（prior gaming day 結束後的 daily snapshot）：

- **有效** iff `A < G` 且 `A ∈ [G − N, G − 1]`（gaming day 閉區間）。
- 等價：`snapshot_age_days = G − A` 須滿足 **1 ≤ snapshot_age_days ≤ N**。
- `A = G`（同日）視為 **無效**（與 `anchor < gaming_day` 一致）。

#### 訓練（Training）

| 面向 | 契約 |
|------|------|
| Mid supplier | Step 3.5 `mid_term_daily_snapshot` + Step 4 `dataset_enrich` **bounded ASOF** |
| ASOF SQL | `anchor_gaming_day < gaming_day`，且 `anchor_gaming_day >= gaming_day - N`；取最新一筆 |
| Baseline mid `fe__*` | 六欄重新啟用：`fe__bets_cnt__w1d`、`fe__wager_sum__w15m_over_w1d`、`fe__wager_cv_w7d`、`fe__payout_odds_z_prior_w30d`、`fe__interarrival__last_gap_z__w7d`、`fe__odds__payout_odds_z__w7d` |
| Audit（**進 model**） | `mid_term_anchor_gaming_day`、`mid_term_snapshot_age_days`、`mid_term_snapshot_missing_flag` |
| 窗外／無 anchor | mid primitive 與 composite **null**；`mid_term_snapshot_missing_flag = 1`；audit 仍寫實際 age（若有 A） |

訓練分佈預期：bounded N=30 下 mid 相關 null 率約 **~15%** 量級（高於舊 unlimited ASOF ~5%）；屬契約取捨，非單純 bug。

#### 生產（Production — scorer v2）

| 面向 | 契約 |
|------|------|
| Mid supplier | Feast online `mid_term_daily_spike_features`（refresh 後） |
| Serving 模式 | **A**：Feast 存每 canonical **最新** anchor 列；scorer 在 Feast attach **之後**依每筆 bet 的 `G` 做窗檢查 |
| Anchor 來源 | Feast online 須可提供 `anchor_gaming_day`（欄位或自 `event_timestamp` 等價反推）；與訓練 audit 公式一致 |
| 窗外／無 anchor | 將該列 **mid primitive + composite** 清為 null；`missing_flag=1`；**仍允許** `predict_proba`（cell null 政策） |
| Composite 順序 | 先 bounded null-out mid primitive，再 `attach_mid_term_composite_columns` |
| 覆蓋 | allowlist canonical Feast 列數 **≥ 95%** |
| 營運 freshness | `anchor_gaming_day_max` vs `expected_anchor_gaming_day`；逾 **3 天** hard cap → 停打；其間可 degraded |

**禁止**：僅改 production 為 finite N、未 retrain 的 unlimited-ASOF 權重（分佈漂移）。  
**禁止**：production 使用 training `bet_id` parquet 或 unlimited carry-forward 當 bounded 契約的替身。

#### 驗證與 promotion gate

1. `feature_cadence_audit.json`：`violation_count = 0`
2. `06_verify_training_serving_parity.py` → `feature_parity_verification.json`，**all-feature gate pass**
3. `deploy_e2e_gate`：`verdict=pass`；`mid_cell_null_rate` 符合 bounded 訓練預期帶
4. 上線後抽樣：`audit_supplier_root_cause` — `feast_online_mid_value_missing` 非主導

同一套 bounded 邏輯須出現在：**`dataset_enrich.py`、scorer（Feast 後處理）、Step 6 offline replay**。

---

### Option A — 僅歷史模型（勿用於新訓練）

適用於已在 **unlimited ASOF** 上訓練、尚未以 Option B retrain 的 bundle（例如 `20260520-032615-df799bd`）。

| 面向 | 訓練 | Production |
|------|------|------------|
| ASOF | 全歷史 `anchor < gaming_day`，無 N 上限 | Carry-forward：每 canonical 最新 anchor；bootstrap + incremental merge |
| 與 Option B 差異 | 分佈較「新鮮」 | 若僅 finite-N refresh 無 carry-forward，會大量 null |

**勿**將 Option B 權重部署在 Option A serving 語意上，或反之。  
詳見 [`Mid-Term Feast Train-Serve Parity Incident - 20260522.md`](Mid-Term%20Feast%20Train-Serve%20Parity%20Incident%20-%2020260522.md)。

---

## Scorer v2 Feast 缺失政策

- **Cell-level NULL**（窗內 structural null、或 Option B 窗外刻意 null）：**允許** `predict_proba`；prediction log 須記錄 per-row missing／degraded。
- **Feast entity row 整列缺失**：**skip 該列** + 可稽核 log；不得當成全 null 特徵打分。
- **批次 entity-missing rate > 10%**：整批 scoring cycle **hard fail**（`scorer_feast_entity_missing_fail_fraction`，預設 0.10）。

---

## Supplier 規則

| 類型 | Production supplier |
|------|---------------------|
| `baseline_model` | ClickHouse／scoring 輸入原始欄位 |
| `feast_trial_1h` | serving PIT builder（online）；trial parquet 不入包 |
| short-term `fe__*` | scorer bounded on-the-fly PIT；`fe_short_term_parquet` 非 production supplier |
| mid-term `fe__*` | **Feast online** + Option B scorer 窗檢查（見上） |
| `patron__*__w180d_m1snap` | **Feast online** slow；Parquet slow 非 production substitute |

Production scorer v2 **不得** fallback 至 `fe_derived_parquet`、`fe_short_term_parquet` 或 training Parquet（明示或靜默）。

---

## 非目標

- 開發建包不要求持有最新 production 資料。
- Training snapshot 不能替代 production readiness。
- 建包不負責重建 production snapshot 或把 snapshot Parquet 拷入 bundle。
- Scorer v2 第一階段不要求 short-term `fe__*` 走 Feast online；short-term 由 bounded PIT 供應。

---

## 相關文件

| 層級 | 文件 |
|------|------|
| Mid-term 事故與 Option A/B 背景 | [`Mid-Term Feast Train-Serve Parity Incident - 20260522.md`](Mid-Term%20Feast%20Train-Serve%20Parity%20Incident%20-%2020260522.md) |
| Serving 事故（2026-05-19） | [`Feature Serving Incident - 20260519.md`](Feature%20Serving%20Incident%20-%2020260519.md) |
| Feast spike | [`Feast Production Feasibility Spike - DECISION_RECORD.md`](Feast%20Production%20Feasibility%20Spike%20-%20DECISION_RECORD.md) |
| Scorer v2 實作計畫 | [`Scorer v2 Feast Runtime - IMPLEMENTATION_PLAN.md`](Scorer%20v2%20Feast%20Runtime%20-%20IMPLEMENTATION_PLAN.md) |
| Feast refresh | [`Feast Online Refresh - IMPLEMENTATION_PLAN.md`](Feast%20Online%20Refresh%20-%20IMPLEMENTATION_PLAN.md) |
| 執行計畫 | [`Scorer v2 Feast Runtime - WORKING_PLAN.md`](Scorer%20v2%20Feast%20Runtime%20-%20WORKING_PLAN.md) |

---

## 決策日誌

| 日期 | 決策 |
|------|------|
| 2026-05-22 | Option A carry-forward 修復 unlimited-ASOF **已部署** 模型之 serving（事故閉環） |
| 2026-05（本 SSOT） | **未來模型預設 Option B**：N=30、Feast + scorer 窗檢查、六 mid + 三 audit 進 baseline、營運 stale hard cap 維持 3 天與 N 分離 |

**待實作（程式尚未完全對齊本 SSOT）**：training bounded ASOF SQL、scorer `apply_mid_term_bounded_asof`、Feast `anchor_gaming_day` 暴露、`config` 中 N=30／bootstrap=30 常數、registry baseline 啟用 mid 欄位。實作完成前以本文件為目標契約。
