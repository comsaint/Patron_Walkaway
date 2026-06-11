# trainer_hightier - `t_casino_txn` Source Integration Implementation Plan

本文件屬於 **Implementation Plan 層**，定義如何落實 `Data pipeline - SSOT.md` **§5.2** 之外部 raw 事件來源 **L0 接入、清洗與 quarantine exit readiness**。  
**In scope**：source-grain cleaned parquet、DQ sidecar、Step 1 manifest 掛鉤（Phase D）、**quarantine exit gate 與 snapshot-scoped source promotion**（Phase E）。  
**Out of scope**：bet-grain feature materialize、registry baseline promotion、Step 3.5 enrich、serving 變更（屬 Feature experimentation 另案）。

**最後更新：** 2026-06-11

---

## 0) 對齊基準與狀態

| 項目 | 內容 |
|------|------|
| SSOT | `trainer_hightier/doc/ssot/Data pipeline - SSOT.md` §5.2 |
| 來源發現 | `doc/FINDINGS.md` **[FND-19]** |
| Raw schema | `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` §5 |
| 參考實作模式 | `utils/bet_l0_preprocess.py`、`utils/session_l0_preprocess.py` |
| Registry 契約 | `contracts/preprocess_l0_data_contract_registry.yaml` |
| **Quarantine** | 上游 data source incident 期間，cleaned artifact 標 **`not_model_eligible`**；不得進 model / registry / Gate 1 promote |
| **Quarantine exit** | SSOT §5.2 **Quarantine exit checklist**（8 gates）；本 IP **Phase E** 定義 realization |
| Feature 實驗 | `Feature experimentation - IMPLEMENTATION_PLAN.md` 之 txn_lite **暫 defer**；歷史 ablation 不作 promote 依據 |
| **里程碑** | **M1** L0 quarantine-ready（Phase A–D）；**M2** source exit-ready（Phase E） |

### 0.1 決策紀錄（已鎖定）

| ID | 決策 | 理由 |
|----|------|------|
| TXN-L0-001 | **Available time** = logical observed-at（物化為 `txn_available_ts`） | 保守 PIT；不假設 `start_dtm` 即時可見，也不允許事件發生前可見 |
| TXN-L0-002 | **Event time** = `start_dtm` | 業務事件時間；L1 PIT join 基準，L0 僅保留 |
| TXN-L0-003 | L0 **保留所有 `type`** | BUYIN/CASHOUT 篩選屬 **L1 materializer**，不在 L0 |
| TXN-L0-004 | Hard exclude：缺 `casino_txn_id`、`start_dtm` 或 `__etl_insert_Dtm` | 無法 dedup / 無法定義時間語意 |
| TXN-L0-005 | Suspicious 列保留 + flag | 供 DQ 與事故調查（例如非正 `txn_value`、非預期 type/status 組合） |
| TXN-L0-005A | raw `__etl_insert_Dtm < start_dtm` 先 preflight；無 correction rule 則 **hard-fail** | raw anomaly 不可靜默通過；有登錄 episode 才能 logical-correct |
| TXN-L0-005B | 登錄 `TXN-BULK-INGEST-2025-05-27` correction；`ingest_delay_cap_sec = 128` | 2025-05-27 bulk noon stamp 導致 tail 2,071 rows raw observed-before-event；用 txn residual P95 cap + event floor |
| TXN-L0-006 | 輸出路徑 `artifacts/cleaned/cleaned__gmwds_t_casino_txn/` | 與既有 cleaned layer 慣例對齊 |
| TXN-L0-007 | Phase D（Step 1 manifest）**第一個 code slice 不做** | 先證明 L0 + DQ 可重跑、可稽核 |
| TXN-L0-008 | **Source quarantine** 至 incident 關閉 | 縮小 blast radius；先接資料、後做 feature |
| TXN-EXIT-001 | **Quarantine exit** 以 SSOT 8 gates 為驗收真相 | 解除 `not_model_eligible` 須 snapshot-scoped explicit decision record |
| TXN-EXIT-002 | **Phase E exit ≠ registry promote** | 來源可信是 L1 / Gate 1 之前提；promotion 屬 Feature experimentation |
| TXN-EXIT-003 | quarantine 期 `txn_lite` / Gate 1 **不得**單獨作 exit 依據 | 僅背景參考；exit 後須 post-quarantine 重跑 |

---

## 1) 實作目標（What to Realize）

### 1.1 L0 cleaned source layer

- 從 raw `t_casino_txn` 分區 Parquet 產出 **source-grain** cleaned parquet。
- Delete-aware dedup：`casino_txn_id` 為 logical key；任一版本 `__op='d'` 或 `__deleted='True'` → 整筆 logical id 排除。
- Dedup 排序：`__etl_insert_Dtm DESC, updated_dtm DESC`。
- 物化欄位（最低）：保留 raw 業務欄 + `txn_event_ts`（= `start_dtm`）+ `txn_observed_at_raw`（= raw `__etl_insert_Dtm`）+ `txn_available_ts`（logical observed-at）+ `observed_at_correction_rule_id` + DQ flags。
- Logical observed-at v1:

```sql
GREATEST(
  LEAST(
    TRY_CAST(__etl_insert_Dtm AS TIMESTAMP),
    TRY_CAST(start_dtm AS TIMESTAMP) + INTERVAL 128 SECOND
  ),
  TRY_CAST(start_dtm AS TIMESTAMP)
)
```

- 預設 raw 輸入：固定根目錄 `data/t_casino_txn`，內含 `partition_YYYYMM/part_*.parquet`。
- Partition discovery 只接受 `partition_YYYYMM` 目錄；不使用 `data/new tables/t_casino_txn__part_*.parquet` 作正式來源。
- Partition 名稱是來源分區 / audit boundary，不等同嚴格 calendar event month；`start_dtm` 可因 casino day cutover spill 到鄰近日。

### 1.2 DQ sidecar

每輪 materialize 至少產出：

| 產物 | 用途 |
|------|------|
| `txn_l0_materialization_report.json` | row counts、dedup 前後、hard exclude 計數、raw observed-before-event preflight evidence、correction rule application、null rate、type 分布、observed−event delay 分布、fingerprint |
| `source_metadata.json` | `cleaning_policy_id`、`source_contract_ref`、`not_model_eligible: true`、raw partition fingerprint、code version |

### 1.3 契約登記

- 在 `preprocess_l0_data_contract_registry.yaml` 新增 `gmwds_t_casino_txn` 條目（logical key、時間欄位、dedup、exclude 規則、輸出 schema 版本）。
- `cleaning_policy_id` 建議：`t_casino_txn_l0_v1_fnd19`（與 FND-19 對齊，但 **不含** L1 type filter）。

---

## 2) 非目標（明確排除）

**Phase A–D / quarantine 期間**

- `materialize_txn_lite.py` 或任何 bet-grain `txn__*` 特徵物化（屬 L1 / feature experimentation）。
- `feature_candidate_registry.yaml` 之 `group_txn_lite_cashflow` promotion 或 baseline 變更。
- Step 3.5 `_ensure_fe_enriched_training_parquet_for_step4()` 之 txn hook。
- Scorer / Feast / serving 路徑變更。
- 依 incident 前 ablation 結果做 model 或 registry 決策。

**Phase E（quarantine exit）仍排除**

- registry baseline promotion、Step 3.5 enrich hook 實作、Gate 1 ablation 執行（屬 Feature experimentation IP/WP）。
- 以 quarantine 期間歷史 `txn_lite` / Gate 1 結果作為 exit 或 promote 的**唯一**依據。
- blanket 解除 `not_model_eligible`（未經 decision record 綁定 snapshot / 月份範圍）。

---

## 3) 模組邊界

```
raw t_casino_txn (partition Parquet)
        │
        ▼
  txn_l0_preprocess.py              ← Phase B
        │
        ├── cleaned__gmwds_t_casino_txn/*.parquet
        ├── txn_l0_materialization_report.json
        ├── txn_l0_preflight_report.json
        └── source_metadata.json (not_model_eligible)
        │
        ▼
  Phase D: manifest / inventory      ← raw→cleaned 追溯
        │
        ▼
  Phase E: exit evidence package     ← 8 SSOT gates 彙整
        │
        ├── completeness / PIT / CDC audits
        ├── business semantics + join readiness
        └── decision record (snapshot-scoped release)
        │
        ╳  (Phase E 不含)
        ▼
  materialize_txn_lite.py → Step 3.5 enrich → registry / Gate 1
```

| 模組 | 職責 | Phase |
|------|------|-------|
| `utils/txn_l0_preprocess.py` | DuckDB/SQL 清洗、dedup、flag、寫 cleaned parquet + sidecar | B–C |
| `utils/txn_l0_schema.py` | DDL ↔ dictionary ↔ L0 cast 契約；schema drift 驗證 | A, E |
| `contracts/preprocess_l0_data_contract_registry.yaml` | 機讀 L0 契約條目 | A |
| `tests/test_txn_l0_preprocess.py` | dedup、hard exclude、suspicious flag、deterministic fingerprint | B–C |
| `utils/partition_inventory.py` | raw / cleaned 分區 inventory | D |
| `feature_experiment/txn_asof_join.py` | PIT audit 工具（`txn_available_ts <= prediction_ts`） | E |
| `feature_experiment/materialize_txn_lite.py` | L1 消費端 partial 排除（**非** Phase E 交付） | — |
| Step 1 manifest hook | partition inventory、raw fingerprint 綁定 | D |
| exit evidence aggregator（待實作） | 跨分區 sidecar 彙整、completeness audit、evidence package | E |
| decision record template | snapshot-scoped governance release | E |

---

## 4) 工作流與階段（Phases）

### Phase A — Source contract freeze（文件 + registry）

**交付**

- SSOT §5.2 已鎖定（見 `Data pipeline - SSOT.md`）。
- `preprocess_l0_data_contract_registry.yaml` 草稿條目：`logical_key`、`event_time`、`available_time`、`dedup_order`、`hard_exclude`、`quarantine_flag`。

**驗收**

- 契約欄位可由 unit test 或 schema validator 讀取；與 FND-19 無矛盾（L0 比 FND-19 **更寬**：全 type）。

### Phase B — L0 preprocess 實作（第一個 code slice）

**交付**

- `trainer_hightier/utils/txn_l0_preprocess.py`（對齊 bet/session L0 慣例：DuckDB、temp 輸出、atomic rename）。
- CLI 或既有 preprocess entrypoint 可指定 raw partition path + output dir。
- `cleaning_policy_id = t_casino_txn_l0_v1_fnd19`。

**驗收**

- 對固定 raw fixture：dedup 結果 deterministic；delete 列整筆 logical id 消失。
- Hard exclude 計數寫入 sidecar。
- raw `__etl_insert_Dtm < start_dtm` 若未被 `TXN-BULK-INGEST-2025-05-27` 等登錄 correction rule 覆蓋 → source-level hard-fail，且輸出 preflight evidence；不得產生 cleaned parquet。
- 符合登錄 correction rule 的列可產生 cleaned parquet，但 `txn_available_ts >= txn_event_ts` 必須成立，並填入 `observed_at_correction_rule_id`。
- Suspicious 列（非正 `txn_value`、非預期 type/status 組合等）保留且有對應 DQ flag。
- `source_metadata.json` 含 `not_model_eligible: true`。

### Phase C — DQ report 與 investigation 就緒

**交付**

- `txn_l0_materialization_report.json` 含：raw rows、post-dedup rows、excluded rows、type histogram、delay percentiles。
- 可選：簡短 markdown 摘要模板（investigation 用，非 SSOT）。

**驗收**

- 同一 raw fingerprint 重跑 → sidecar fingerprint 一致。
- DQ 報告可供 incident 關閉前之資料品質審查（不需跑 model）。

### Phase D — Step 1 manifest / partition inventory（後續 slice）

**交付**

- Raw partition 與 cleaned output 綁定至 `source_manifest_v2`（或等價 inventory）。
- Cache invalidation 鍵含 raw shard fingerprint。

**驗收**

- manifest 可追溯「哪批 raw → 哪批 cleaned」；與 Cache Redesign SSOT 對齊。

**備註：** 第一個 code slice **不做** Phase D。

### Phase E — Quarantine Exit & Source Promotion

承接 SSOT §5.2 **Quarantine exit checklist**（8 gates）。本 phase 定義 **source exit readiness** 與 **snapshot-scoped** 解除 `not_model_eligible` 的 realization strategy；**不等同** registry promotion 或 L1 feature 啟用。

#### Entry criteria（M2 進入條件）

- **M1 達成**：Phase A–C DoD 完成；L0 sidecar 可重跑、可稽核。
- **上游 incident closure**：data source incident 已正式關閉（外部輸入；非本 IP 可單獨判定）。
- **Phase D 建議完成**：raw→cleaned 可追溯（`source_manifest_v2` 或等價 inventory）；若未完成，decision record 須明示 traceability 風險與補齊計畫。
- **Preflight / correction 治理成形**：registry-driven CDC 與 observed-before-event correction 無未登錄 episode。

#### In-scope deliverables（8 gates → implementation artifacts）

| SSOT gate | Implementation deliverable | 模組 / 產物類型 | 驗收口徑 |
|-----------|---------------------------|-----------------|----------|
| **1. Schema contract** | 三角一致驗證管線 | `schema/schema.txt` ↔ dictionary §5 ↔ `txn_l0_schema.py`；`assert_txn_l0_schema_matches_ddl()`、`assert_dictionary_section5_matches_ddl()`；fingerprint 寫入 materialization report | 65 欄位順序、型別、nullable/default、L0 cast 零 drift；CI gate 通過 |
| **2. Source completeness** | 跨分區 completeness audit | 彙總各月 `source_metadata.json` / materialization report 之 `partition_coverage`、`is_partial_partition`、`partial_partition_reasons`；產出 **snapshot-scoped 合格月份清單** | 擬 exit snapshot **不得**靜默納入 partial 月；partial 月已補齊重跑，或 decision record 明示排除 |
| **3. CDC correctness** | CDC / correction closure 證據包 | registry episodes + `txn_l0_preflight_report.json` + correction application counts | delete-aware dedup 穩定；未登錄 observed-before-event episode = 0；無未解釋 CDC 邏輯漂移 |
| **4. Time semantics / PIT safety** | PIT 安全驗證 | 時間語意契約（event=`txn_event_ts`、observed raw、available=`txn_available_ts`）；delay 分布；`txn_asof_join.py` 對 **已核准 snapshot** 之正式 PIT audit 結果 | `txn_available_ts >= txn_event_ts` 不變式成立；無未受控 observed-before-event 或 PIT leakage |
| **5. Business semantics** | L1 業務規則契約 + domain review | `type` / `status` / `sub_type` / `buyin_status` 納入排除矩陣；錨點 FND-19、dictionary §5；按 type/status/sub_type 之 DQ 切片 | `BUYIN`、`CASHOUT`、`Prize Redemption`、`SUBMITTED+SUCCESS` 等邊界有明示納入/排除決策與 domain sign-off |
| **6. Join / entity readiness** | 首個 model-eligible use case 規格 | join grain（`player_id` + as-of 時間；**非** `bet_id` / `session_id`）；key coverage 摘要；`player_id` vs `canonical_id` 決策 | use-case 說明 + coverage 摘要 + key 選擇 rationale 已鎖定或 decision record 明示 defer 範圍 |
| **7. Promotion evidence boundary** | 證據邊界政策 | 明列採納 / 排除證據來源；quarantine 期 `txn_lite` / Gate 1 僅背景參考 | decision record 不得僅引用 quarantine 期實驗結果作 exit 依據 |
| **8. Governance release** | snapshot-scoped release 機制 | `doc/decisions/t_casino_txn Quarantine Exit - DECISION_RECORD.md`；sidecar / registry 標記由 decision 綁定月份範圍 | 明示適用 snapshot、允許用途、已知排除項、批准人、回退條件；**非** blanket release |

#### Exit evidence package（Phase E 核心交付物）

單一可稽核目錄或 manifest，彙整上述 8 gates 之證據，供 explicit decision record 引用。最低必含：

| 區塊 | 內容 |
|------|------|
| `schema_contract/` | drift 驗證結果、fingerprint、`schema_ddl_ref` |
| `completeness_audit/` | 各月 partial 狀態、合格 / 排除月份清單 |
| `cdc_correction/` | preflight reports、correction counts、episode registry 快照 |
| `pit_safety/` | delay 分布、PIT audit 結果、rule id 追溯 |
| `business_semantics/` | 規則矩陣、DQ 切片、domain sign-off 或等價紀錄 |
| `join_readiness/` | use-case spec、coverage 摘要、entity key 決策 |
| `evidence_boundary/` | 採納 / 排除證據來源清單 |
| `decision_record_link` | 指向 governance release 文件與適用 snapshot 範圍 |

建議路徑：`trainer_hightier/artifacts/quarantine_exit/t_casino_txn/<snapshot_id>/`（具體命名由 Working plan 定義）。

#### Out-of-scope boundary（Phase E 明確不含）

- bet-grain `txn__*` materialize 實作與擴充（L1 → Feature experimentation IP）。
- `feature_candidate_registry.yaml` baseline 變更、Step 3.5 enrich hook、Gate 1 ablation 執行。
- decision record 內之具體批准內容（屬 governance 產物，非 IP 正文）。
- Working plan 級 task 分解、owner 指派、CLI / pytest 指令。

#### Exit criteria（M2 達成條件）

- 8 個 SSOT gates 均有對應 deliverable 且 evidence package 完整。
- explicit decision record 已建立，且 exit scope 為 **snapshot-scoped**。
- `not_model_eligible` 解除僅適用於 decision record 綁定之 snapshot / 月份範圍；新分區預設仍 quarantine 直至重新審核。
- post-exit L1 啟動條件已指向 Feature experimentation IP/WP（§1.7），但 **不在本 phase 執行**。

---

## 4.1) 里程碑（Milestones）

| 里程碑 | 對應 Phase | 定義 | 主要產物 |
|--------|------------|------|----------|
| **M1: L0 quarantine-ready** | A–D | L0 可在 quarantine 內 ingest、preprocess、DQ、investigation | cleaned parquet、sidecar JSON、（可選）manifest 綁定 |
| **M2: source exit-ready** | E | 滿足 SSOT 8 gates；來源可被視為 **model-eligible 前提** | exit evidence package、decision record、snapshot-scoped release 標記 |

**關係**：M1 ≠ M2。完成 L0 slice 不代表可解除 quarantine；M2 需要額外證據包與 governance release。

**依賴鏈**：

```
M1 (Phase A–C) → Phase D (traceability) → Phase E (8 gates + evidence package) → decision record
                                                                              ↓
                                                    Feature experimentation: post-quarantine L1 / Gate 1
```

---

## 5) 風險與緩解

| 風險 | 緩解 |
|------|------|
| 上游 incident 未關閉，cleaned 資料不可信 | `not_model_eligible` + quarantine；僅 DQ / investigation；Phase E entry 要求 incident closure |
| L0 與 L1（txn_lite）清洗語意混淆 | SSOT 分層 §5.2；L0 全 type，L1 才 BUYIN/CASHOUT；Phase E gate 5 鎖 business semantics |
| `player_id` vs `canonical_id` 不一致 | Phase E gate 6 列為顯式決策點；不得默默跳過 |
| Raw 分區格式被誤判 | contract 只接受固定 `data/t_casino_txn/partition_YYYYMM/part_*.parquet` 結構 |
| 既有分區存在 raw `__etl_insert_Dtm < start_dtm` | L0 preflight 必須揭露；符合登錄 episode 才 correction；Phase E gate 3 稽核 closure |
| 與既有 experiment materializer 重複 dedup 邏輯 | L1 消費 L0 cleaned，不重讀 raw |
| **Partial partition 誤納入 model-eligible snapshot** | Phase E gate 2 completeness audit；L1 消費端 fail-closed 排除 partial 月 |
| **Business semantics 未簽核即做 L1 promote** | Phase E gate 5 要求 domain sign-off；exit ≠ registry promote |
| **以 quarantine 期實驗結果誤當 promote 證據** | Phase E gate 7 證據邊界；decision record 明列採納/排除 |
| **`not_model_eligible` 無 snapshot 邊界、難回退** | Phase E gate 8 snapshot-scoped release + 回退條件 |
| **Phase D 未完成即 exit** | Phase E entry 建議 D 完成；否則 decision record 須明示 traceability 風險 |

---

## 6) 驗證策略

### Phase A–D（M1）

- **Unit tests**：dedup、delete-aware、hard exclude、raw observed-before-event uncovered hard-fail、covered correction、`txn_available_ts >= txn_event_ts`、flag、sidecar schema、partial partition。
- **Schema drift tests**：`assert_txn_l0_schema_matches_ddl()`、`assert_dictionary_section5_matches_ddl()`。
- **Smoke**：對 `data/t_casino_txn/partition_202505`（含 known correction episode）與 `partition_202605` 跑 preprocess。
- **禁止**：以 quarantine 期產出跑 Gate 1 或更新 registry baseline。

### Phase E（M2）

- **Schema contract gate**：三方 drift 測試全通過；fingerprint 寫入 evidence package。
- **Completeness audit**：跨分區彙總 partial 狀態；合格月份清單與 decision record 一致。
- **CDC closure audit**：preflight + correction counts；未登錄 episode = 0。
- **PIT safety validation**：對已核准 snapshot 執行 formal PIT audit（`txn_asof_join.py` 或等價）；delay 分布審查。
- **Business semantics review**：DQ 切片 + domain sign-off 納入 evidence package。
- **Evidence package completeness**：8 gates 均有對應 artifact；decision record 連結有效。
- **禁止**：僅以 quarantine 期 `txn_lite` / Gate 1 結果作為 M2 exit 依據。

---

## 7) 與其他文件邊界

| 文件 | 關係 |
|------|------|
| `Data pipeline - SSOT.md` §5.2 | 治理真相；Quarantine exit checklist 8 gates |
| `Data pipeline - IMPLEMENTATION_PLAN.md` | gaming_day_event migration；**不**承載 txn L0 / exit |
| `Feature experimentation - IMPLEMENTATION_PLAN.md` | L1 特徵與 Gate；**post-exit** 啟動條件 |
| `Feature experimentation - WORKING_PLAN.md` §1.7 | 歷史 txn_lite 步驟；**paused / quarantine**；exit 後恢復 L1 |
| `t_casino_txn Source Integration - WORKING_PLAN.md` | 本 IP 之 **execution 拆解**（M1 Phase A–D；M2 Phase E quarantine exit） |
| `doc/decisions/t_casino_txn Quarantine Exit - DECISION_RECORD.md` | Phase E gate 8 **必要產物**；決策內容不在 IP 正文 |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` §5 | 人類可讀 schema；Phase E gate 1 三角一致 |

**三層紀律（本 IP 應遵守）**

| 層 | 本主題內容 | 不應混入 |
|----|------------|----------|
| **SSOT** | quarantine 規則、8 gates、snapshot-scoped exit | task 分解、CLI 指令 |
| **Implementation plan（本文件）** | phases、deliverables、milestones、模組邊界、驗收標準 | owner 指派、逐月 smoke 順序 |
| **Working plan** | W1–W4 / Phase E task 表、DoD checkbox、blocker playbook | 重寫 SSOT 業務規則 |
| **Decision record** | 批准人、適用月份、回退條件、採納/排除證據 | 架構設計、ticket 拆解 |

---

## 8) 定義完成（DoD）

### 8.1 M1 — 第一個 code slice（Phase A–C）

- [ ] `txn_l0_preprocess.py` 可從 raw partition 寫入 `cleaned__gmwds_t_casino_txn/`
- [ ] `preprocess_l0_data_contract_registry.yaml` 有 `gmwds_t_casino_txn` 條目
- [ ] raw source discovery 固定支援 `data/t_casino_txn/partition_YYYYMM/part_*.parquet`
- [ ] raw `__etl_insert_Dtm < start_dtm` uncovered hard-fail 有測試與 sidecar evidence
- [ ] `TXN-BULK-INGEST-2025-05-27` covered correction 有測試；`txn_available_ts >= txn_event_ts`
- [ ] sidecar JSON 含 `not_model_eligible: true`
- [ ] unit tests 通過
- [ ] **未**修改 registry baseline、Step 3.5、production trainer、serving

### 8.2 M2 — source exit-ready（Phase E）

- [ ] SSOT 8 gates 均有 implementation deliverable 與 evidence package 對應區塊
- [ ] schema contract drift 測試全通過
- [ ] completeness audit 完成；partial 月已補齊或 decision record 明示排除
- [ ] CDC / correction closure 稽核通過；未登錄 episode = 0
- [ ] PIT safety validation 對已核准 snapshot 完成
- [ ] business semantics 有 domain sign-off 或等價紀錄
- [ ] join / entity readiness 規格已鎖定或 defer 範圍已明示
- [ ] decision record 已建立；exit scope 為 snapshot-scoped
- [ ] **未**以 quarantine 期 `txn_lite` / Gate 1 作為唯一 exit 依據

---

*文件版本：v2（L0 + Phase E quarantine exit）；L1 feature crafting 待 M2 達成後由 Feature experimentation WP 恢復。*
