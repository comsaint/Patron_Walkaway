# Feature Supplier Contract — SSOT

本文件為 `trainer_hightier` **feature supplier contract** 的治理真相（SSOT）。

它回答：active model 的每個 feature 由誰供應、production 需要哪些 runtime resources、deploy 前必須通過哪些 validator 與 gate。  
本文件**不包含** ticket 級實作拆解、衝刺排程或程式修改步驟。

若與 [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) 在 scorer 行為上衝突，**以 Scorer Runtime Contract 為準**；本文件補充 supplier 治理層，不重寫 scorer runtime 細節。

**最後更新：** 2026-06-15

---

## 1) 目標與商業目的

- 讓 active model 的 feature list 可**機械化**映射到 production supplier、runtime resource 與 deploy gate，避免「訓練 OK、package 通過、production deploy 才 fail」。
- 新增 feature 或 supplier 時，CI / package / deploy preflight 必須在**上線前**暴露缺 source、缺欄位、schema drift、freshness / coverage 不足等問題。
- 禁止 production 依賴 training/package 內的 absolute path 或 legacy Parquet supplier 作為主路徑。

---

## 2) 範圍與非範圍

### In scope

- Feature supplier taxonomy 與 `runtime_supplier` 治理規則。
- Governing SSOT 來源與 derived artifact（`deploy_contract.json`）關係。
- Runtime config 政策（`.py` SSOT、`.env` 邊界）。
- Validator 強制要求與 contract gate 嚴格度。
- Per-model deploy contract 的 acceptance criteria。

### Out of scope

- Scorer v2 的 mid/slow Option B、Feast refresh、bounded ASOF 等 runtime 細節（見 Scorer Runtime Contract）。
- `t_casino_txn` short-PIT runtime 的 implementation 步驟（見 [`t_casino_txn Short-PIT Runtime - IMPLEMENTATION_PLAN.md`](../implementation/active/t_casino_txn%20Short-PIT%20Runtime%20-%20IMPLEMENTATION_PLAN.md)）。
- 整體 feature supplier contract 的 code 落地、validator 實作、CI wiring（另案 implementation / working plan）。
- 訓練管線 L0 cleaning、FQG、Gate 1/2 候選篩選（見 [`Data pipeline - SSOT.md`](Data%20pipeline%20-%20SSOT.md)）。

---

## 3) 利害關係人

- **ML / 特徵工程**：新增 feature 時必須同步更新 registry、supplier 分類與 validator。
- **Platform / deploy ops**：依 package 產出的 `deploy_contract.json` 與 preflight 結果確認 production readiness。
- **Scorer 維護者**：確保 supplier implementation 與 contract 一致，禁止 silent fallback。

---

## 4) Governing SSOT（真相來源）

### 4.1 機械化 SSOT（code / bundle artifacts）

| 來源 | 角色 | 說明 |
|------|------|------|
| **Frozen feature registry** | Feature → metadata | Bundle 內 `feature_candidate_registry.snapshot.yaml`；含 `source`、`time_horizon`、`runtime_supplier`、`runtime_inputs` |
| **Supplier taxonomy** | Supplier 分類 | 本文件 §5；registry `runtime_supplier` 必須映射到 taxonomy |
| **Supplier requirement map** | Supplier → runtime resources | 定義每個 supplier 需要的 CH 表、Feast layer、bundle artifact、schema 欄位 |
| **Validator registry** | Supplier → validator | 每個 production `runtime_supplier` 必須有對應 validator；缺 validator 即 contract violation |
| **`build_scorer_supplier_plan()`** | Model → plan | [`feature_supply.py`](../../serving/feature_supply.py) 從 frozen registry + `model.pkl` 產出 `ScorerSupplierPlan` |

### 4.2 Derived artifact（非 SSOT）

| 產物 | 產生時機 | 用途 |
|------|----------|------|
| **`deploy_contract.json`** | **Package time** | 從 frozen registry + supplier plan + requirement map **生成**；供 ops / deploy preflight 讀取 |
| **`active_manifest.json`** | Package time | Metadata-only；可標記 `deploy_requires_*` 等旗標，但不得成為人工維護的 supplier 真相 |

**禁止**：手動編輯 `deploy_contract.json` 作為 supplier 真相；禁止在 production host 以 ad-hoc path 覆寫繞過 contract。

### 4.3 文件 SSOT（本目錄）

本文件為 **governance SSOT**：定義 supplier contract 的原則、taxonomy、gate 政策與 acceptance criteria。  
Implementation 細節與 ticket 拆解放在 `doc/implementation/` 與 `doc/working/`，不得反向改寫本文件 scope。

---

## 5) Supplier Taxonomy（初版）

Production scorer v2 的 supplier 分為下列 taxonomy。**Registry `runtime_supplier` 必須可映射到其中一類**；新增類別須更新本 SSOT 與 validator registry。

| Taxonomy | Registry `runtime_supplier`（現行） | Production runtime source | Mechanism |
|----------|--------------------------------------|---------------------------|-----------|
| **`clickhouse_raw`** | `clickhouse_raw` | ClickHouse `t_bet`（scoring 輸入） | 當筆 bet row passthrough |
| **`short_term_pit`** | `short_term_pit_builder` | ClickHouse `t_bet` hot pool | Batch-scoped bounded PIT compute |
| **`short_term_pit`** | `txn_lite_builder` | ClickHouse `t_casino_txn` | Batch-scoped bounded PIT compute（與 `t_bet` short-PIT 對齊；**不得**依賴 deploy host 手動放置 cleaned parquet） |
| **`feast_online_mid`** | `feast_online_mid` | Bundle-local Feast online mid layer | Startup refresh + bounded ASOF |
| **`feast_online_slow`** | `feast_online_slow` | Bundle-local Feast online slow layer | Startup refresh + monthly anchor |
| **`mid_composite`** | `composite` | Score-time derived | 依 registry `runtime_inputs` 從 mid / short / bet 衍生 |
| **`bundle_static_artifact`** | （mapping / allowlist） | Bundle `mapping/*.parquet` | Frozen join keys；建包時入 bundle |
| **`offline_only`** | — | Training / parity / backtest artifacts | **禁止**作為 production scorer 主 supplier |

### 5.1 與 Scorer Runtime Contract 的對齊

- **`bet__*` 與 short `fe__*`**：皆屬 **`short_term_pit`**；registry `source: feast_trial_1h` 為歷史標籤，production 走 live PIT。
- **Mid / long `fe__*` / `patron__*`**：分別屬 **`feast_online_mid`** / **`feast_online_slow`**；Parquet snapshot **不是** production substitute。
- **Training PIT cache**（`fe_short_term_parquet`）：屬 **`offline_only`**；禁止當 production 主路徑。
- **`txn__*`**：屬 **`short_term_pit`**（`txn_lite_builder`）；production 讀 ClickHouse，training / parity 可讀 cleaned parquet（implementation 層分離，contract 層 taxonomy 一致）。

---

## 6) Per-Model Supplier Contract

對每一個 active model bundle，contract 必須可機械化推導：

```text
model.pkl.feature_columns
  → frozen registry lookup
  → build_scorer_supplier_plan()
  → ScorerSupplierPlan (per-supplier column buckets)
  → supplier requirement map (runtime resources)
  → validator registry (per-supplier checks)
  → deploy_contract.json (derived, package-time)
```

### 6.1 Contract 必須回答的問題

對 model 中每個 feature column：

1. **誰供應？** — `runtime_supplier` / inferred supplier
2. **用什麼 source？** — ClickHouse 表、Feast FV、bundle parquet、score-time composite
3. **何時驗證？** — package gate、deploy e2e gate、deploy preflight、score-time smoke
4. **失敗時怎麼辦？** — **hard fail**（見 §8）；不 silent fallback

### 6.2 `ScorerSupplierPlan` 欄位桶（現行 code SSOT）

[`feature_supply.py`](../../serving/feature_supply.py) 的 `ScorerSupplierPlan` 為 per-model routing 的 code SSOT：

- `baseline_cols` → `clickhouse_raw`
- `short_term_cols` → `short_term_pit_builder`
- `txn_cols` → `txn_lite_builder`
- `feast_mid_cols` → `feast_online_mid`
- `feast_slow_cols` → `feast_online_slow`
- `mid_composite_cols` → `composite`
- `unknown_cols` → **contract violation**（必須為空）

Mapping / ADT allowlist 由 bundle 靜態 artifact 供應，不進 `ScorerSupplierPlan` 欄位桶，但 deploy preflight 仍須驗證其存在。

---

## 7) Runtime Config Policy

### 7.1 正式 runtime config：`.py` SSOT

- Production feature generation 的 **resource binding**（例如 bundle Feast repo path、source DB 名稱、CH connection profile）由 **Python runtime config** 定義（例如 `HightierServingConfig` + deploy bundle override）。
- **不得**以 deploy-time config 改變 production feature **算法**或 PIT 語意；算法真相在 supplier implementation + frozen registry。
- Runtime `.py` config **不應**暴露「調整 feature 怎麼算」的 knobs；若無法想像合法的手動調整需求，就不應存在該 config 欄位。

### 7.2 `.env` 邊界

| 允許 | 禁止 |
|------|------|
| ClickHouse credentials | 用 env 切換 supplier 類型 |
| Emergency override（明確標記、可稽核） | 用 env 改 feature window / PIT 規則 |
| Connection endpoints（若 policy 允許） | 用 env 指向 training/package absolute data path 作為 production 主 source |

### 7.3 Path 政策

- Production **不得**讀取 `site-packages/trainer_hightier/artifacts/...` 或 dev repo path 作為 external data root。
- Bundle-local path（`feast_repo/`、`mapping/`、`artifacts/feast/`）為允許的 deploy-scoped root。
- ClickHouse / Feast online 為 score-time 或 startup refresh 的 production source；cleaned parquet 僅 training / offline / parity。

---

## 8) Validator Requirement 與 Contract Gate 嚴格度

### 8.1 Validator 強制

**每一個 production `runtime_supplier` 必須有 registered validator。**

新增 supplier 時：

1. 更新 supplier taxonomy（若為新類別）。
2. 實作 validator（schema、可服務性、sample join / smoke）。
3. 接入 package gate、deploy e2e gate、deploy preflight。

**缺 validator → CI / package / deploy gate hard fail。** 不允許「先上線、之後補驗證」。

### 8.2 Validator 最低要求（existence 不足）

Validator 不得只做「目錄存在」檢查。最低須覆蓋：

| Supplier 類型 | 最低驗證 |
|---------------|----------|
| `clickhouse_raw` / `short_term_pit` | Required columns smoke query；batch player coverage |
| `txn_lite_builder` | CH `t_casino_txn` schema；PIT 欄位；sample 產出全部 `txn__*` |
| `feast_online_mid` / `slow` | Readiness、anchor freshness、schema、cell-null / entity coverage |
| `mid_composite` | Dependency closure 可解析；composite implementation 存在 |
| `bundle_static_artifact` | Parquet 存在、required columns、row count > 0 |

### 8.3 Gate 嚴格度

**Hard fail**（不允許 silent fallback 或 degraded 替代缺失 supplier）：

- 缺 source、缺 required 欄位
- Schema drift（registry / materializer / Feast FV 不一致）
- Freshness / coverage 低於 contract 下限
- `unknown_cols` 非空
- Production 使用 `offline_only` artifact 作為主 supplier

**Warning**（可記錄、可 audit，但不替代 hard fail）：

- Parity 邊界 case 小比例 diff（須在 implementation plan 定義 tolerance）
- Known late-arrival 造成的 bounded window 差異

與 [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) 一致：**禁止** production fallback 至 legacy Parquet supplier。

### 8.4 Gate 時機

| 階段 | 必須驗證 |
|------|----------|
| **Registry / CI** | 新 `runtime_supplier` 有 validator；taxonomy 可映射 |
| **Package** | `build_scorer_supplier_plan` 無 unknown；產出 `deploy_contract.json` |
| **Deploy e2e** | 模擬 production host 的 supplier readiness（含 CH / Feast / bundle artifacts） |
| **Deploy preflight** | 與 e2e 同一套 validator；Feast refresh 後、scorer 啟動前 fail-fast |
| **Score time** | 不 silent 補齊缺失 model columns；missing 須可稽核 |

**禁止**：僅在 `validation_stage="package"` 跳過 runtime disk / CH schema 檢查，導致 production 才 fail。

---

## 9) `deploy_contract.json` Policy

### 9.1 產生

- **時機**：`build_deploy_package`（或同等 package pipeline）。
- **輸入**：frozen registry snapshot、`model.pkl.feature_columns`、`ScorerSupplierPlan`、supplier requirement map。
- **輸出**：唯讀 derived artifact；寫入 deploy bundle。

### 9.2 內容（contract-level，非 implementation）

至少包含：

- Model id / version、feature count
- Per-supplier column lists（或 hash）
- Required runtime resources（CH tables、Feast layers、bundle artifacts）
- Validator ids / gate steps 對照
- Flags：`deploy_requires_clickhouse`、`deploy_requires_feast_online`、`deploy_requires_*`

### 9.3 使用

- Deploy ops / `deploy.main` preflight 讀取，對照 live environment。
- **不得**人工編輯後當作 SSOT；若與 frozen registry 不一致，以 registry + code plan builder 為準。

---

## 10) 成功標準 / Acceptance Criteria

對任一 intended-for-production model bundle：

1. **100% routable**：`ScorerSupplierPlan.unknown_cols` 為空；model 每欄有明確 supplier。
2. **100% validated**：plan 中每個 supplier 類型均有 registered validator，且 deploy e2e + preflight 已執行。
3. **No package path leakage**：production 不讀 wheel / repo absolute training artifact path。
4. **Derived contract present**：bundle 含 machine-readable `deploy_contract.json`，與 plan 一致。
5. **New supplier discipline**：新增 `runtime_supplier` 無 validator 時，package **必須 fail**。

Active reference model `20260613-162313-3eb8de4`（42 features）為驗收基準：42/42 可路由；supplier 分佈與 [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) §Supplier 規則一致。

---

## 11) 已知風險與治理原則

| 風險 | 治理原則 |
|------|----------|
| 新 feature 只改 `model.pkl`、未改 registry | Package gate 必須 fail on unknown_cols |
| 新 supplier 只改 code、未加 validator | CI 必須 fail on missing validator registration |
| Training artifact path 滲入 production | Path SSOT 在 `.py` config；禁止 env 指向 package dir |
| `deploy_contract.json` 人工 drift | 僅 package 生成；preflight 以 registry + plan builder 為準 |
| Parity 通過但 production source 不同 | Validator 須分 training vs production source；parity gate 明確標註 source pair |

---

## 12) 相關文件

| 層級 | 文件 |
|------|------|
| Scorer runtime 行為 | [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) |
| 離線訓練四層 | [`Data pipeline - SSOT.md`](Data%20pipeline%20-%20SSOT.md) |
| `t_casino_txn` short-PIT implementation | [`t_casino_txn Short-PIT Runtime - IMPLEMENTATION_PLAN.md`](../implementation/active/t_casino_txn%20Short-PIT%20Runtime%20-%20IMPLEMENTATION_PLAN.md) |
| Supplier plan code | [`feature_supply.py`](../../serving/feature_supply.py) |
| Deploy e2e gate | [`deploy_e2e_gate.py`](../../serving/deploy_e2e_gate.py) |
| Serving 事故（2026-05-19） | [`Feature Serving Incident - 20260519.md`](../incidents/Feature%20Serving%20Incident%20-%2020260519.md) |

---

## 13) 決策日誌

| 日期 | ID | 決策 |
|------|-----|------|
| 2026-06-15 | FSC-001 | 建立 Feature Supplier Contract SSOT；與 Scorer Runtime Contract 分層，不混 implementation task |
| 2026-06-15 | FSC-002 | `deploy_contract.json` 僅為 package-time derived artifact；governance SSOT 在本目錄 + code registry |
| 2026-06-15 | FSC-003 | 正式 runtime config 用 `.py`；`.env` 僅 credentials / emergency override |
| 2026-06-15 | FSC-004 | 新增 `runtime_supplier` 必須有 validator；缺 validator 則 CI / package / deploy hard fail |
| 2026-06-15 | FSC-005 | Contract gate 嚴格模式：缺 source / schema / freshness / coverage → hard fail |
| 2026-06-15 | FSC-006 | Supplier taxonomy 初版固定；`txn__*` / `txn_lite_builder` 歸 `short_term_pit`，production 走 ClickHouse PIT |
| 2026-06-15 | FSC-007 | Production 不得依賴手動放置 cleaned `t_casino_txn` partition；cleaned parquet 限 training / offline / parity |

---

## 14) Open Questions

| ID | 問題 | 預設 / 備註 |
|----|------|-------------|
| OQ-001 | Supplier requirement map 與 validator registry 的 code 落點（單檔 vs 模組） | Implementation plan 決定；SSOT 只要求「必須存在且可機械查詢」 |
| OQ-002 | `deploy_contract.json` schema version | 首版 implementation 定義；SSOT 要求 versioned、向後可 audit |
| OQ-003 | Emergency override 的允許清單與稽核 | 僅 connection / credential；override 須寫 deploy log |

**下一步（非本文件）**：撰寫 Feature Supplier Contract **Implementation Plan**，落地 requirement map、validator registry、`deploy_contract.json` generator 與 gate wiring。
