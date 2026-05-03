# 分層資料資產與 run/trip — Execution Plan（Working Plan）

> **文件層級**：Working / Execution Plan（執行層）。  
> **目的**：把 SSOT 與 Implementation Plan 落成**可執行任務**（順序、owner 角色、依賴、產物、DoD、gate、升級規則）。  
> **依據**：[`ssot/layered_data_assets_run_trip_ssot.md`](ssot/layered_data_assets_run_trip_ssot.md)（v1.5）、[`implementation plan/layered_data_assets_run_trip_implementation_plan.md`](implementation%20plan/layered_data_assets_run_trip_implementation_plan.md)（v0.5）、[`schema/time_semantics_registry.yaml`](schema/time_semantics_registry.yaml)、[`package/deploy/models/feature_spec.yaml`](package/deploy/models/feature_spec.yaml)。  
> **邊界**：本檔**不重寫**業務定義與架構決策；若與上層文件衝突，以上層為準並回寫本檔。

---

## 0) 執行摘要與狀態圖例

### 0.1 執行摘要

本輪執行目標為：建立與 `trainer` **並行**之分層資料產線（L0→preprocess→L1→L2→publish→可選 online delta），並以 **manifest、determinism、100% feature 覆蓋、correction log** 作為可驗收交付。Phase 1 **不**產出 trip 最終語義；trip v1 於 Phase 2 一次到位。

**Phase 1 進度速記（2026-05-03 更新；含 2026-05-02 smoke）**：**LDA-E1-01** **✅**（見下段 smoke）。**LDA-E1-02** **✅（MVP）**：`scripts/preprocess_bet_v1.py` + `layered_data_assets/preprocess_bet_v1.py`／`l1_paths.py`；輸出 `data/l1_layered/<snap>/t_bet/gaming_day=.../cleaned.parquet` 與同目錄 `manifest.json`（`preprocessing_rule_id`／`preprocessing_rule_version`）；未餵 dummy／eligible sidecar 時 `preprocessing_gaps` 列明 **BET-DQ-02／03** 略過。**LDA-E1-03** **✅（MVP）**：`scripts/materialize_run_fact_v1.py` + `run_id_v1`／`run_fact_v1`；輸出 `data/l1_layered/<snap>/run_fact/run_end_gaming_day=.../run_fact.parquet` + `manifest.json`；`run_id` 與 `ORDER BY payout_complete_dtm ASC, bet_id ASC` 切 run 及 §4.1 canonical JSON 一致（見單元測試）。**LDA-E1-04** **✅（MVP）**：`scripts/materialize_run_bet_map_v1.py` + `run_bet_map_v1`；`run_bet_map.parquet` 與 `run_fact` 共用邊界暫存表語意；單元測試驗證 membership 與 `bet_count`／首尾 bet 序一致。**LDA-E1-05** **✅（MVP）**：`scripts/materialize_run_day_bridge_v1.py` + `run_day_bridge_v1`；以 **`bet_gaming_day`** 分區之 `run_day_bridge.parquet`（distinct `run_id` 等）；可支援日粒度影響分析與重算範圍掃描（單元測試）。**LDA-E1-06** **✅（MVP）**：`ingestion_delay_summary_v1`（DuckDB 預演：`payout_complete_dtm` vs `__etl_insert_Dtm`）+ `manifest_lineage_v1`（fingerprint → `source_hashes`）；已接到 preprocess 與三個 run 物化 CLI；可選 `scripts/manifest_lineage_preview_v1.py` 後補既有 manifest。**LDA-E1-07** **✅（MVP）**：`layered_data_assets/oom_runner_v1.py`（§7.1：輸入位元組／可用 RAM 提示、`memory_limit`／`threads` 階梯、RSS 觀測、JSONL run log、耗盡時 fail-fast + 失敗上下文 JSON）；`scripts/preprocess_bet_v1.py` 與三個 `materialize_run_*_v1.py` 共用 CLI（`--duckdb-run-log`、`--duckdb-oom-failure-context`、`--duckdb-oom-max-attempts`、`--duckdb-initial-memory-limit-mb`）；範例 `schema/examples/oom_run_log.example.jsonl`、`schema/examples/oom_failure_context.example.json`；單元測試 mock OOM 驗證重試與非 OOM fail-fast。**LDA-E1-08** **✅（MVP）**：`layered_data_assets/l1_determinism_gate_v1.py`（§8.1 Gate 1：多部 DuckDB `memory_limit`／`threads` 下重跑物化，比對列數與 row-level `sha256(string_agg…))` fingerprint）；`scripts/gate1_l1_determinism_v1.py` 輸出 JSON 報告、exit code 反映是否一致；`make check-lda-l0` 含 `test_l1_determinism_gate_v1`（`run_fact`／`run_bet_map`／`run_day_bridge`）。**LDA-E1-09** **✅（MVP，2026-05-03）**：`materialization_state` DuckDB + `lda_l1_gate1_day_range_v1.py` 的 **`--state-store`**／**`--resume`**／**`--force`**／**`--stop-after-date`**（預設 state：`data/l1_layered/materialization_state.duckdb`）；詳見 RUNBOOK §5.1。**LDA-E1-10** **✅（MVP，2026-05-03）**：G7 整合測 **`tests/integration/test_lda_e1_10_resume_g7_v1.py`**（一次跑完 vs `stop-after-date`+`--resume`；比對 preprocess+三物化之 row fingerprint）。**LDA-E1-11** **✅（MVP，2026-05-03）**：`preprocess_bet_ingestion_fix_registry_v1.py` + preprocess／CLI 選用 `--ingestion-fix-registry-yaml`（122s **`BET-INGEST-FIX-004`** cap、`__etl_insert_Dtm_synthetic`、manifest `ingestion_fix_*`／`applied_fix_rules`）；`check-lda-l0` 已含 `test_preprocess_bet_ingestion_fix_registry_v1`。**待補**：帶 registry 之 `cleaned` 與 **E1-08** Gate1 同跑並留 JSON／exit 紀錄（見 §5.3 **列 12**）。本機 smoke：`python scripts/l0_ingest.py --data-root data --table t_bet --partition-key gaming_day --partition-value smoke-2026-05-02 --source data/baseline_for_baseline_models.parquet`（先 `--dry-run` 再實寫）；產物 `data/l0_layered/snap_187e491186316d9a24316f86e06dc6b2/snapshot_fingerprint.json` 與 `.../t_bet/gaming_day=smoke-2026-05-02/part-000.parquet`。**刻意**使用 repo 內較小之真實 Parquet（約 1.7MB）以驗證指紋／`source_snapshot_id`／複製路徑；**未**對 ~22GB 之 `gmwds_t_bet.parquet` 做全檔 ingest（避免 OOM／磁碟與時間風險）。治理見 `doc/l0_ingest_governance_decisions.md`。  
**語義同步註記（2026-05-03；2026-05-03 補述）**：上層已升版為 **SSOT v1.5** / **Implementation Plan v0.5**。除 v1.4 起之 run 定義（「30 分鐘 gap + `GAMING_DAY_START_HOUR` 硬切（目前 03:00，Asia/Hong_Kong）」）外，新增 **LDA-014**：殘差 **P95 cap** 定義邏輯 `observed_at_logical`（`t_bet` 目前 **122 sec**，見 `schema/preprocess_bet_ingestion_fix_registry.yaml`）；**L0 raw 不改寫**。**刻意維持不變**：`preprocess_bet_v1` 仍 **`PARTITION BY bet_id`** 去重，輸出主序仍 **`ORDER BY payout_complete_dtm, bet_id`**（與 `run_fact_v1`／scorer 穩定排序對齊）；cap 僅用於邏輯 observed／ingest-delay 摘要，不取代事件序。trip 語義維持「3 個完整 `gaming_day` 無 bet 才關」，實作允許以「3 個完整 `gaming_day` 無 run」等價判定（需一致性驗證報告）。E1 任務的既有 MVP 產物需依新 `definition_version` 進行同步重算與驗收（含 `is_hard_cutoff` 或等價邊界欄位）。

### 0.2 狀態圖例（本檔維護）

| 符號 | 意義 |
| :---: | :--- |
| **✅** | 已滿足該列 DoD 與對應 gate。 |
| **🟡** | 部分完成：有 MVP 或草稿，但未滿 DoD 或缺 CI／證據鏈。 |
| **⏳** | 進行中。 |
| **⬜** | 未開始。 |

### 0.3 Owner 角色（role-based）

| 角色 | 職責摘要 |
|------|----------|
| **Data Platform** | L0/L1 物化、分區、DuckDB／Parquet、OOM 參數、管線編排。 |
| **DS / Feature Owner** | asset-layer `feature_spec`、parity、覆蓋矩陣、特徵語意對齊。 |
| **ML Platform** | CI、schema 驗證、artifact 目錄規範、版本鍵。 |
| **Model Owner** | pilot／adopt 簽核、與訓練目標衝突時裁決。 |
| **Ops / Orchestration** | 排程、環境、保留／GC（與 backlog 對齊）。 |

---

## 1) 執行基線與前置條件

### 1.1 必備輸入（凍結前不得宣稱 Phase 0 完成）

- SSOT v1.5 可取得且為爭議解方之最高優先序（見 SSOT §0.1）。
- Implementation plan v0.5 可取得（含 Executive Summary、§6.1.1 枚舉規則、§7.1 OOM、§8.1 gate、§10 correction log、§4 `observed_at_logical`／ingestion fix registry 契約）。
- `package/deploy/models/feature_spec.yaml` 可解析（YAML AST）。
- `schema/time_semantics_registry.yaml` 存在且可被 CI 讀取。

### 1.2 Ready to Start（Phase 0）

- 已指派各 Phase 的 **Data Platform** 與 **DS / Feature Owner** 對口窗口。
- 已選定 **artifact 根目錄**（例如 `artifacts/layered_data_assets/`）與命名慣例（本檔不定死路徑，但每任務 DoD 必須寫出實際路徑）。

---

## 2) 執行目標與成功定義

### 2.1 全程成功定義（對齊 implementation plan §8.1）

1. **Determinism**：同 `source_snapshot_id`、不同 §7.1 執行參數組合下，L1（及 Phase 3 起之 L2）**hash／列數**一致；並完成約定之 **row-level canonical hash**（抽檢或全量）。
2. **Lineage**：任一批次可自 manifest 追溯到 L0 分區與 preprocessing 版本。
3. **Membership**：`trip_run_map`／`run_bet_map` 可完整重建 run／trip 邊界（Phase 2 起為 gate）。
4. **Ingestion**：`published` 批次皆含 **ingestion_delay_summary**；published 缺失率為 0。
5. **Feature**：deploy `feature_spec.yaml` 依 **§6.1.1** 枚舉之全部 **`(track_section, feature_id)`** 皆覆蓋且與 asset-layer／L2 **deterministic 一致**。
6. **Resume/Idempotency**：同日期區間在「一次跑完」與「中斷後續跑」兩種路徑下，輸出列數與 row-level hash 一致；已成功分區可安全跳過。

### 2.2 非目標（本輪不強制）

- 不強制本輪完成 **線上 scorer** 讀取 `late_arrival_correction_log`（見 implementation plan §10.2）。
- 不在本輪決定 **K/T/D 最終數值**、**L0 不可變儲存**實作、**trainer Step 6/7 取代與否**（見 §11 backlog）。

---

## 3) 工作分解結構（WBS）總覽

| Phase | 主題 | 關鍵產物 |
|------|------|----------|
| **0** | 契約與 schema freeze | registry 審核流程、preprocess 規格、manifest／correction_log schema、feature dependency registry 初稿 + CI |
| **1** | L1 MVP | L0、`run_fact`、`run_bet_map`、`run_day_bridge`、manifest 預演、Gate 1 + OOM |
| **1R** | L1 resumable + G7 | `materialization_state`、E1-09、E1-10、日粒度 stop/resume |
| **2** | Trip + published | `trip_fact`、`trip_run_map`、published snapshot、late fixture、ingestion gate |
| **3** | Feature + L2 | asset-layer spec、L2、parity、coverage matrix、mismatch ledger 收斂 |
| **4** | 治理與整合決策 | KPI 儀表、trainer／chunk cache 整合決策包、rollout |

---

## 4) Phase 0 — 契約與 Schema Freeze

### 4.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ✅ | **LDA-E0-01** | `time_semantics_registry` PR 流程：template、必填欄位、與 schema dict／FND 對照檢查表 | ML Platform + Data Platform | §1.1 | `.github/` 或 `doc/` 下 PR checklist +（可選）`scripts/validate_time_semantics_registry.py` | 本機：`python scripts/validate_time_semantics_registry.py`；合併前仍建議設 required check |
| ✅ | **LDA-E0-02** | Preprocessing 規格書：`preprocess_*_v1` 與 FND-01/03/11/13 對照 | DS / Feature Owner + Data Platform | E0-01 | `doc/preprocessing_layered_data_assets_v1.md`（路徑可調，須寫入 repo） | 每條規則有 rule id；與 manifest 可引用欄位對齊 |
| ✅ | **LDA-E0-03** | Manifest schema：SSOT §8 + `ingestion_delay_summary` | ML Platform | SSOT | `schema/manifest_layered_data_assets.schema.json`（或等價） | JSON Schema 或表格可機器驗證；範例 `manifest.json` 通過驗證 |
| ✅ | **LDA-E0-04** | `late_arrival_correction_log` schema：對齊 implementation plan §10 + manifest join 鍵 | ML Platform | E0-03 | `schema/late_arrival_correction_log.schema.json` + 範例列 | PK／索引欄位與 §10.1 一致；範例通過驗證 |
| ✅ | **LDA-E0-05** | Feature enumerator：依 §6.1.1 產出 `features_enumerated.json`（穩定排序） | ML Platform + DS | `feature_spec.yaml` | `artifacts/.../features_enumerated.json` + `scripts/enumerate_deploy_features.py`（或等價） | `make check-layered-contracts` 內含枚舉與 artifact 一致性 |
| ✅ | **LDA-E0-06** | Feature dependency registry 初稿：每 `(track_section, feature_id)` 一列 | DS / Feature Owner | E0-05 | `artifacts/.../feature_dependency_registry.csv`（或 yaml） | 欄位含：所需 L1 欄位、是否允許回掃 bet、計算來源占位；無缺列（細部 `TBD` 由 DS 後續收斂） |
| ✅ | **LDA-E0-07** | Phase 0 CI gate：registry + manifest + correction_log schema + enumerator | ML Platform | E0-01–E0-06 | CI workflow 或 `make check-layered-contracts` | 本機：`make check-layered-contracts`；遠端 CI 由團隊自設 |

**Phase 0 完成條件**：E0-01–E0-07 皆 **✅**。

---

## 5) Phase 1 — L1 MVP（無 trip 最終語義）

### 5.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ✅ | **LDA-E1-01** | L0 ingest：分區 raw、`source_snapshot_id`、分區 hash 規則 | Data Platform | Phase 0 | 同上 + `scripts/l0_ingest.py` + `layered_data_assets/l0_fingerprint.py` + `schema/examples/snapshot_fingerprint.example.json` + `doc/l0_ingest_governance_decisions.md`；CI：`/.github/workflows/layered_data_assets.yml` | 同一輸入重跑得相同 `source_snapshot_id`；**2026-05-02** 本機真檔 smoke（`baseline_for_baseline_models.parquet` → `snap_187e491186316d9a24316f86e06dc6b2`；見 §0.1 速記） |
| ✅ | **LDA-E1-02** | Preprocess job：輸出清洗後 bet 流／表 + rule id 寫 manifest | Data Platform | E0-02, E1-01 | `scripts/preprocess_bet_v1.py`、`layered_data_assets/preprocess_bet_v1.py`、`layered_data_assets/l1_paths.py`、`schema/examples/manifest_preprocess_bet_l1_example.json` | `cleaned.parquet` + `manifest.json`；`preprocessing_rule_id`=`preprocess_bet_v1`；**MVP**：dummy／rated sidecar 可選；未提供時 manifest `preprocessing_gaps` 註記 **BET-DQ-02／03** |
| ✅ | **LDA-E1-03** | `run_fact` 物化：`run_id` hash 依 implementation plan §4.1（含首筆 `bet_id`） | Data Platform | E1-02 | `layered_data_assets/run_id_v1.py`、`run_fact_v1.py`、`scripts/materialize_run_fact_v1.py`、`l1_paths.l1_run_fact_partition_dir`、`schema/examples/manifest_run_fact_l1_example.json`；主分區 **`run_end_gaming_day`**（SSOT §5.2）；切 run 採 **30 分鐘 gap + `GAMING_DAY_START_HOUR` 硬切**（目前 03:00）並輸出 `is_hard_cutoff`（或等價欄位） | Gate 1 自動化見 **LDA-E1-08**；本任務 DoD：同輸入 DuckDB `sha256` 與 Python `derive_run_id` 一致（單元測試）、hard cutoff 邊界 fixture 通過、manifest 通過 schema |
| ✅ | **LDA-E1-04** | `run_bet_map` membership | Data Platform | E1-03 | `layered_data_assets/run_bet_map_v1.py`、`scripts/materialize_run_bet_map_v1.py`、`l1_paths.l1_run_bet_map_partition_dir`、`schema/examples/manifest_run_bet_map_l1_example.json`；輸出 `run_bet_map.parquet`（`run_id`, `bet_id`, `player_id`, …）與 manifest | 可由 map 還原每 run 之 bet 集合；與 `run_fact` 之 `bet_count`／首尾 `bet_id` 一致（單元測試） |
| ✅ | **LDA-E1-05** | `run_day_bridge`：日粒度影響分析 | Data Platform | E1-03 | `layered_data_assets/run_day_bridge_v1.py`、`scripts/materialize_run_day_bridge_v1.py`、`l1_paths.l1_run_day_bridge_partition_dir`、`schema/examples/manifest_run_day_bridge_l1_example.json`；輸出 `run_day_bridge.parquet`（`bet_gaming_day` 分區鍵，SSOT §5.2） | 對任意 `bet_gaming_day` 分區可列出該日受影響 `run_id` 集合，供重算範圍掃描（單元測試） |
| ✅ | **LDA-E1-06** | Manifest writer：每批次 `manifest.json` + ingestion 摘要（預演） | Data Platform + ML Platform | E0-03, E1-02 | `layered_data_assets/ingestion_delay_summary_v1.py`、`manifest_lineage_v1.py`；`scripts/preprocess_bet_v1.py` 與 `scripts/materialize_run_*_v1.py` 寫入／合併；`scripts/manifest_lineage_preview_v1.py` 後補 | `make check-lda-l0` 含新單元測試；manifest 仍通過 schema；`source_hashes` 與 fingerprint 銜接見 `doc/l0_ingest_governance_decisions.md` |
| ✅ | **LDA-E1-07** | OOM runner：實作 §7.1（估算、監控、階梯重試、fail-fast、run log） | Data Platform | implementation plan §7.1 | `layered_data_assets/oom_runner_v1.py`；CLI 旗標見 preprocess／`materialize_run_*_v1`；`schema/examples/oom_run_log.example.jsonl`、`oom_failure_context.example.json` | `make check-lda-l0` 含 `test_oom_runner_v1`；mock OOM 重試成功、非 OOM fail-fast；執行參數僅影響資源路徑（G6） |
| ✅ | **LDA-E1-08** | Gate 1 自動化：同 snapshot 多組執行參數 + row hash | ML Platform | E1-03–E1-07 | `layered_data_assets/l1_determinism_gate_v1.py`、`scripts/gate1_l1_determinism_v1.py`、`tests/unit/test_l1_determinism_gate_v1.py` | `make check-lda-l0`：三部 L1 產物在多組 DuckDB 資源設定下列數與 row fingerprint 一致；CLI 可寫 JSON 報告（exit 0/1） |
| ✅ | **LDA-E1-09** | 日粒度 resumable 編排：state store + 原子寫入 + `--resume`/`--force` | Data Platform | E1-02–E1-06 | **`schema/materialization_state.schema.sql`**、**`layered_data_assets/materialization_state_store_v1.py`**、**`scripts/lda_l1_gate1_day_range_v1.py`**（`--state-store`／`--resume`／`--force`／`--stop-after-date`）、**`layered_data_assets/RUNBOOK.md` §5.1**、**`tests/unit/test_materialization_state_store_v1.py`** | **MVP（2026-05-03）**：DuckDB state；`--resume` 同 `input_hash` 則 skip 各步 subprocess；`--force` 重跑；預設 state 路徑見 RUNBOOK；**`--stop-after-date`** 可日中斷演練。**待補**：子程序內 Parquet **tmp→rename** 若尚未全面，仍以各 CLI 為準；**LDA-E1-10** G7 kill/resume 自動測試。 |
| ✅ | **LDA-E1-10** | Resume Gate 自動化：中斷/續跑一致性測試 | ML Platform | E1-09 | **`tests/integration/test_lda_e1_10_resume_g7_v1.py`**（G7：`lda_l1_gate1_day_range_v1` 一條龍 vs `--stop-after-date` + `--resume`）；**`cleaned_bet_parquet_row_fingerprint`** in **`layered_data_assets/l1_determinism_gate_v1.py`**；`make check-lda-l0` 納入該 integration | **MVP（2026-05-03）**：兩連續日、四 L1 Parquet（preprocess + 三物化）**row_count + row fingerprint** 與 Gate1 既有函式一致；**未**加獨立 kill/SIGINT CI job（可選後補） |
| ✅ | **LDA-E1-11** | Preprocess 升級：接入 `schema/preprocess_bet_ingestion_fix_registry.yaml`，實作 `observed_at_logical`（`t_bet` **`ingest_delay_cap_sec=122`**）、manifest `ingestion_fix_*`／`applied_fix_rules`；`ingestion_delay_summary` 改以 synthetic observed 計算（見 SSOT §4.4 **LDA-014**） | Data Platform | E1-02, E1-06, SSOT v1.5 | **`layered_data_assets/preprocess_bet_ingestion_fix_registry_v1.py`** + 更新 `preprocess_bet_v1.py`、`scripts/preprocess_bet_v1.py`、`tests/unit/test_preprocess_bet_ingestion_fix_registry_v1.py`、`schema/examples/manifest_preprocess_bet_l1_example.json`、`Makefile`（`check-lda-l0` 納入 registry 測試） | **不變**：`PARTITION BY bet_id`；輸出主序仍 `payout_complete_dtm, bet_id`；dedup 在傳入 registry 時 `ORDER BY __etl_insert_Dtm_synthetic`；**選用** `--ingestion-fix-registry-yaml`（及可選 `--ingestion-fix-registry-version-expected`）。**MVP 落地 2026-05-03**：`validate_layered_contracts` + `check-lda-l0` pytest **80 passed**。**待補證據**：§5.3 **列 12**—以同一輸入帶 cap 與 **LDA-E1-08** Gate1 同跑並記錄 JSON／exit（PR smoke）。 |

**Phase 1 完成條件**：E1-01–E1-08 皆 **✅**；**不**要求 `trip_fact` 最終語義。  
**Phase 1 延伸（例行 smoke／PR）**：**LDA-E1-11** 已 **✅（MVP）**；請將「帶 `--ingestion-fix-registry-yaml` 之 preprocess + manifest 欄位 +（建議）Gate1 同跑」納入合併前檢查清單；仍不阻塞 E1-01–E1-08 之已達標敘述。  
**Phase 1R（resumable 擴充）完成條件**：**E1-09**、**E1-10** 皆 **✅（MVP，2026-05-03）**；G7 由 **`tests/integration/test_lda_e1_10_resume_g7_v1.py`** 覆蓋（`make check-lda-l0`）。可選加強：專用 **SIGINT**／多 worker 併發 CI job。  
**E1-11（ingest cap）驗收建議**：合併主線前與 **LDA-E1-08** 同跑至少一組 DuckDB profile（**含**帶 registry 之 preprocess 路徑時，補列 12）；若 `cleaned` 因 dedup tie-break（`__etl_insert_Dtm_synthetic`）導致下游列集合變化，須在 PR／實作說明中列明並以 fixture 或對照表覆蓋預期。

### 5.2 `LDA-E1-09` / `LDA-E1-10` / `LDA-E1-11` 交付細化

#### `LDA-E1-09`（resumable 編排）最低交付

- state schema 檔：`schema/materialization_state.schema.sql`（或等價）
- runner/CLI：`scripts/lda_l1_day_range_resume_v1.py`（或在既有 `scripts/lda_l1_gate1_day_range_v1.py` 擴充 `--state-store`／`--resume`／`--force`）
- 支援旗標（最低集合）：`--date-from`、`--date-to`、`--resume`、`--force`、`--stop-after-date`（或等價「僅跑 N 日後退出」）、`--state-store`
- **狀態鍵**：至少 `(source_snapshot_id, artifact_kind, partition_day)`；`partition_day` 與各產物 Hive 分區一致（`gaming_day` / `run_end_gaming_day` / `bet_gaming_day` 依 artifact 對照表記錄於 RUNBOOK）
- 原子寫入：`*.tmp` → `rename`；僅在產物與 manifest 皆寫成功後才標 `status=succeeded`
- 狀態追蹤：可查 `status`、`attempt`、`input_hash`、`output_uri`、`error_summary`、`updated_at`（與 implementation plan §2.3 state store 敘述對齊）
- **與編排器關係**：日迴圈內呼叫順序建議固定為 `preprocess_bet_v1` → `run_fact` → `run_bet_map` → `run_day_bridge` →（可選）`gate1`；state 須能標記**任一步**失敗而不誤標後續步為成功

#### `LDA-E1-10`（Resume Gate）最低測試集合

- 測試 A：一次跑完（baseline：`row_count` + 約定 **row_hash**／fingerprint 每 `(artifact_kind, partition_day)`）
- 測試 B：跑到中途以 `--stop-after-date` 或人為 `SIGINT` 中斷，再 `--resume` 跑完
- 驗證：A/B 的每個 `(artifact_kind, partition_day)` 輸出 `row_count` 與 `row_hash` **一致**（G7）
- 驗證：已 `succeeded` 分區在 `--resume` 下為 **`skipped`**；`--force` 可重算且 state 顯示新 `attempt`／新 `input_hash` 觸發
- **覆蓋面**：至少 2 個連續 `gaming_day`、每日常態路徑四產物；Gate1 若納入 resume 路徑，失敗時不得留下半套 `succeeded`

#### `LDA-E1-11`（preprocess ingest P95 cap）最低交付

**狀態（本檔維護）**：**✅ MVP（2026-05-03）**—下列 bullets 與主程式對齊；**§5.3 列 12（Gate1+cap）**仍建議補本機／CI 紀錄。

- **Registry 接線**：`--ingestion-fix-registry-yaml`（選用；未傳則維持舊行為）+ `--ingestion-fix-registry-version-expected`（可選 fail-fast 鎖 `registry_version`）；registry 解析／契約不一致時 **fail-fast**（實作名稱以 `scripts/preprocess_bet_v1.py --help` 為準）
- **SQL 語意**（`t_bet`）：在 `filtered` 與 `ranked` 之間插入衍生欄  
  `__etl_insert_Dtm_synthetic = LEAST(TRY_CAST(__etl_insert_Dtm AS TIMESTAMP), TRY_CAST(payout_complete_dtm AS TIMESTAMP) + INTERVAL 122 SECOND)`（秒數與 registry 中 `ingest_delay_cap_sec` 一致；實作以 registry 為準）
- **dedup**：`ROW_NUMBER() … ORDER BY __etl_insert_Dtm_synthetic DESC NULLS LAST, bet_id DESC`（**仍** `PARTITION BY bet_id`）
- **輸出排序**：`ORDER BY payout_complete_dtm ASC NULLS LAST, bet_id ASC`（**不**改為 observed 主序）
- **Manifest**：寫入 `ingestion_fix_rule_id`／`ingestion_fix_rule_version`、`applied_fix_rules`（至少 `BET-INGEST-FIX-004:v1`）、可選 `fix_registry_sha256`；`preprocessing_gaps` 若 registry 缺欄則列明
- **`ingestion_delay_summary`**：`compute_ingestion_delay_summary_preview` 之 `observed_at_col` 改為 `__etl_insert_Dtm_synthetic`（或等價參數化），使摘要反映 cap 後語意
- **驗收**：單元測試覆蓋「delay 超過 cap → synthetic 觸頂」「未超過 → synthetic 等於 raw」；Gate1 在相同輸入下仍通過（run 邊界不依 observed 排序）

### 5.3 Phase 1R + E1-11 — 子任務拆解與估時（execution checklist）

以下為 **E1-09／E1-10** 與 **E1-11** 落地用工作分解；**估時為單人 person-day 量級**（E1-09 與 E1-11 可由不同開發者並行）。實作可優先擴充既有 `scripts/lda_l1_gate1_day_range_v1.py`，或另開 `scripts/lda_l1_day_range_resume_v1.py`（見 §5.2）。

| 狀態 | 序 | 子任務 | Owner | 估時 | 依賴 | 完成定義（DoD） |
| :---: | :---: | :--- | :--- | :---: | :--- | :--- |
| ✅ | 1 | `schema/materialization_state.schema.sql`（或 DuckDB init SQL）落地 + 文件化欄位語意 | ML Platform + Data Platform | 0.5 | E0-03 | **2026-05-03**：`schema/materialization_state.schema.sql`；`ensure_materialization_state_schema` 於編排器啟動時執行 |
| ✅ | 2 | state store 讀寫模組（`pending→running→succeeded|failed`、attempt 遞增） | Data Platform | 1.0 | 1 | **2026-05-03**：`materialization_state_store_v1.py`（`running`／`succeeded`／`failed`、`attempt`）；併發鎖 **未**做（後續可選） |
| ✅ | 3 | `input_hash` 計算規則固定化（例：`sha256` 串接 `source_snapshot_id`、該日 L0/preprocess 路徑、`definition_version`、`transform_version`、相關 manifest `source_hashes`） | Data Platform | 0.5 | E1-02 | **2026-05-03**：穩定 JSON + sha256（輸入檔 stat、fingerprint 原文、`cleaned` stat）；hash 變更則不 skip（隱式 stale） |
| 🟡 | 4 | 產物原子寫入包裝（`*.tmp` → rename；失敗不落 `succeeded`） | Data Platform | 0.5 | 2 | **編排層**：子程序 exit 0 後方標 `succeeded`。**子程序內** tmp→rename 仍依各 CLI 現況；全鏈條原子寫待收斂 |
| ✅ | 5 | 編排器／CLI 接上 `--resume` / `--force` / `--stop-after-date` / `--state-store` | Data Platform | 1.5 | 2–4 | **2026-05-03**：`lda_l1_gate1_day_range_v1.py` |
| ✅ | 6 | `layered_data_assets/RUNBOOK.md`（或本檔 §5.2）補操作範例與故障排除 | Data Platform | 0.25 | 5 | **2026-05-03**：RUNBOOK §5.1 |
| ✅ | 7 | `LDA-E1-10`：fixture 資料 + kill/resume 測試 +（可選）CI workflow | ML Platform | 1.5 | 5 | **2026-05-03**：`tests/integration/test_lda_e1_10_resume_g7_v1.py`（`--stop-after-date` 代替 SIGINT）；G7 指紋比對 preprocess+三物化；CI workflow **可選** |
| ⬜ | 8 | Phase 2 預留：trip 物化日編排**應沿用**同一 state 契約（E2-01 起） | Data Platform | 0.25 | 5 | execution plan / implementation plan 已註記；實作 PR 可引用本列 |
| ✅ | 9 | **E1-11**：YAML registry 載入與驗證（path、版本、`ingest_delay_cap_sec`、active rule id） | Data Platform | 0.5 | E1-02 | **2026-05-03**：`preprocess_bet_ingestion_fix_registry_v1.py` + 單元測試；契約不一致／FIX-004 未啟用時 fail-fast |
| ✅ | 10 | **E1-11**：DuckDB SQL 插入 `__etl_insert_Dtm_synthetic` + dedup `ORDER BY` 改為 synthetic | Data Platform | 1.0 | 9 | **2026-05-03**：`preprocess_bet_v1.py`；dedup 僅在傳入 registry 時使用 synthetic；`test_preprocess_bet_v1_ingestion_cap_changes_dedup_winner` |
| ✅ | 11 | **E1-11**：manifest 欄位 + `ingestion_delay_summary` 改 observed 欄位 + 範例 manifest 更新 | Data Platform + ML Platform | 0.75 | 10, E1-06 | **2026-05-03**：manifest 寫入 `ingestion_fix_*`／`applied_fix_rules`；cap 啟用時 summary 用 `__etl_insert_Dtm_synthetic`；範例 JSON 已更新；`validate_layered_contracts` + `check-lda-l0` 通過 |
| ⬜ | 12 | **E1-11**：Gate1 迴歸（含 OOM profiles）證明 run 產物不變或僅預期內變更 | ML Platform | 0.5 | 11, E1-08 | 待補：以 `--ingestion-fix-registry-yaml` 產出之 `cleaned` 餵後續物化 + Gate1；文件記錄「預期不變」之判準；CI 或本機指令可重跑 |
| ⬜ | 13 | **E1-12**：`cleaned` 單一活躍資料集策略落地（按 `gaming_day` 覆寫，不保留大量歷史 parquet） | Data Platform + Ops | 0.75 | E1-09, E1-11 | 文件化並實作路徑慣例：固定 active root、分區 `*.tmp -> rename` 覆寫、同步更新 state；不得改變既有業務語義 |
| ⬜ | 14 | **E1-13**：最小追溯與回滾保障（輕量變更索引 + 最近一次回滾點） | Data Platform + ML Platform | 1.0 | 13 | 每次分區覆寫都寫事件索引（含 `gaming_day`、`input_hash`、`row_count`、`updated_at`、operator）；可對單日執行一次回滾演練並附證據 |

**Phase 1R（E1-09+10）合計（粗估）**：約 **5.5–6.5 person-days**（含測試）；若兩人並行 schema+state 與 CLI，wall-clock 約 **3–4 工作天**。  
**E1-11 加計（粗估）**：約 **2.75–3.5 person-days**（列 9–12）；**列 9–11 已關**（2026-05-03）；**列 12** 待 Gate1+cap 證據。與 E1-09 並行時 wall-clock 取較長分支 + 合併驗收約 **0.5 天**。  
**E1-12/E1-13 加計（粗估）**：約 **1.75–2.5 person-days**（列 13–14）；若與 E1-11 並行，建議先凍結 active root／索引 schema 再做回滾演練。

**與既有腳本對齊（建議）**

- 首選：在 `scripts/lda_l1_gate1_day_range_v1.py` 外層包一層「按日迴圈 + state」，避免重複維護三套 materialize 呼叫。
- 備選：獨立 `scripts/lda_l1_day_range_resume_v1.py` 僅負責 orchestration，內部仍呼叫既有 preprocess／materialize／gate1。

---

## 6) Phase 2 — Trip v1 + Published Snapshot

### 6.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E2-01** | `trip_fact`：3 個完整 `gaming_day` 關閉語義（實作可用「無 run」等價判定） | Data Platform | Phase 1 + **Phase 1R** | `trip_fact` | SSOT「無 bet」語義 fixture 通過，且「無 bet vs 無 run」判定一致性報告通過；日編排建議沿用 **E1-09** state 契約 |
| ⬜ | **LDA-E2-02** | `trip_run_map` membership | Data Platform | E2-01 | `trip_run_map` | trip→run 完整可重建 |
| ⬜ | **LDA-E2-03** | `trip_id` hash：§4.1 `first_run_id` 錨定 | Data Platform | E2-01 | ID 規則單元／整合測試 | 同 snapshot 重跑 trip_id 不變 |
| ⬜ | **LDA-E2-04** | Publisher：`published_snapshot_id`、sidecar manifest、回滾策略 | Data Platform + Ops | E0-03, E2-01 | `published_snapshot.json` + 目錄慣例文件 | 可指回上一版 snapshot；發布流程文件化 |
| ⬜ | **LDA-E2-05** | Published ingestion：`ingestion_delay_summary` 強制 | Data Platform | E2-04 | published 批次 manifest | **缺失率 = 0** |
| ⬜ | **LDA-E2-06** | `late_arrival_correction_log` writer + fixture | Data Platform | E0-04, E2-04 | correction log 範例 + SSOT 對齊測試 | late bet／correction fixture 下 log 與 ID 變化符合預期 |
| ⬜ | **LDA-E2-07** | K/T/D 提案文件：數值建議 + 負載評估（不定最終值） | DS + Data Platform | SSOT §5.4 | `doc/ktd_proposal_layered_data_assets.md` | 有候選值與評估方法；標註「需 Model Owner／Ops 簽核」 |

**Phase 2 完成條件**：E2-01–E2-07 皆 **✅**；Gate 3–4（membership、ingestion）對 published 路徑成立。

---

## 7) Phase 3 — Feature Coverage + L2

### 7.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E3-01** | asset-layer `feature_spec`：B 方案、`player_id` 分區語意 | DS / Feature Owner | E0-05, E0-06 | `package/.../feature_spec_asset_layer.yaml`（路徑依 repo 慣例） | 不含 `canonical_id` 作為主分區鍵；與 deploy 枚舉 1:1 列 |
| ⬜ | **LDA-E3-02** | `run_fact` 欄位擴充：由 registry 驅動最小集合 | Data Platform + DS | E3-01 | 更新後 `run_fact` schema 文件 + 產物 | registry 每列所需欄位皆可從 L1 取得或記錄例外 |
| ⬜ | **LDA-E3-03** | L2 assemble：窗、索引、（可選）抽樣僅在此層 | Data Platform | E3-02 | L2 parquet／矩陣目錄 | L2 manifest 指涉 `feature_version`／`transform_version` |
| ⬜ | **LDA-E3-04** | Reference recompute：依 deploy spec **獨立**重算參考值 | DS + Data Platform | E3-03 | 參考輸出目錄 + 重現指令 | 與 trainer 快取解耦；指令文件化 |
| ⬜ | **LDA-E3-05** | `parity_validator`：reference vs L2 deterministic diff | ML Platform + DS | E3-04 | diff 報告 + mismatch ledger | 任一差異進 ledger；無 silent pass |
| ⬜ | **LDA-E3-06** | Coverage matrix：registry + 狀態欄匯出 | DS | E0-06, E3-05 | `coverage_matrix.csv` | 100% 列；鍵為 `(track_section, feature_id)` |
| ⬜ | **LDA-E3-07** | Mismatch ledger 收斂至 0 open | DS / Feature Owner | E3-06 | `mismatch_ledger.csv`（或 issue 連結欄） | **Gate 5** 滿足：無 open mismatch |

**Phase 3 完成條件**：E3-01–E3-07 皆 **✅**。

---

## 8) Phase 4 — 治理與 Trainer 整合決策

### 8.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E4-01** | KPI 儀表或週報：Reuse rate、Recompute ratio、TTR(p95)、ingestion coverage | Ops + Data Platform | Phase 2–3 | 儀表連結或週報模板 | 指標定義與資料來源可追溯 |
| ⬜ | **LDA-E4-02** | 離線重算 job 雛形：讀 correction log + manifest 決定重算範圍 | Data Platform | E2-06 | job spec + dry-run 報告 | 文件化輸入／輸出；不依賴線上 scorer |
| ⬜ | **LDA-E4-03** | Trainer／chunk cache／Step 6-7 **整合決策包** | Model Owner + DS + Data Platform | Phase 3 | `doc/trainer_layered_assets_integration_decision.md` | 明確選項：合併／取代／雙軌；前置條件與回滾 |
| ⬜ | **LDA-E4-04** | Rollout：shadow → pilot → adopt 檢核表 | Model Owner | E4-03 | `doc/rollout_checklist_layered_assets.md` | adopt 前須書面簽核欄位 |

**Phase 4 完成條件**：E4-01–E4-04 皆 **✅**（採「持續演進」；狀態可長期維持 🟡 但需記錄原因）。

---

## 9) Cross-Phase Gates（橫向驗收）

| Gate | 內容 | 主要驗證時機 |
|------|------|----------------|
| **G1 Determinism** | 同 snapshot、不同 §7.1 參數；hash／列數；row-level canonical hash | Phase 1 起持續；Phase 3 含 L2 |
| **G2 Lineage** | manifest → L0／preprocess／版本鍵 | Phase 1 起 |
| **G3 Membership** | `run_bet_map`、`trip_run_map` 可重建邊界 | Phase 1（run）、Phase 2（trip） |
| **G4 Ingestion** | published 批次 `ingestion_delay_summary` 完整 | Phase 2 起 |
| **G5 Feature** | §6.1.1 全量 `(track_section, feature_id)` 覆蓋 + deterministic 一致 | Phase 3 |
| **G6 OOM Invariant** | 執行參數僅影響資源／時間，不影響語義輸出 | Phase 1 起與每次大表變更 |
| **G7 Resume Invariant** | 一次跑完 vs 中斷續跑結果一致；成功分區可安全 skip | Phase 1 起（E1-09 完成後） |

**阻塞規則**：任一 Gate 失敗，**禁止**進入下一 Phase 的「對外宣稱完成」狀態；可並行準備下一 Phase 程式，但不得 merge 為 production-ready。

---

## 10) Cadence、風險與升級

### 10.1 Cadence

- **每週**：Phase owner 更新本檔任務表狀態欄（✅／🟡／⏳／⬜）。
- **每次發布 published snapshot 前**：跑 G2–G4 最小檢查套件。
- **每次更動 `feature_spec.yaml` 或 asset-layer spec**：重跑 E0-05 enumerator + coverage diff。
- **每次 merge 影響 L1 日編排／state store 邏輯**：至少跑 G1 + **G7**（或等價之 E1-10 子集）。

### 10.2 風險與升級（摘要）

| 風險 | 徵兆 | 升級動作 |
|------|------|----------|
| OOM 頻發 | 重試耗盡、單日分區失敗 | Data Platform 降窗／加分桶；記錄峰值；必要時凍結更大窗需求至 Phase 4 |
| `GAMING_DAY_START_HOUR` 與來源 `gaming_day` 口徑漂移 | run 邊界異常抖動、`is_hard_cutoff` 比例異常 | 視為 `definition_version` 變更事件；凍結發布、開升版重算任務，並回寫 SSOT/implementation plan |
| resumable state 損毀或不一致 | 成功分區被覆寫、失敗分區被誤跳過 | 啟用 state/manifest 雙重校驗；原子寫入；`--force` 僅允許顯式重算 |
| trip「無 bet」與「無 run」判定漂移 | 關閉時點偏移，影響 trip_id 與特徵 | 維持語義以「無 bet」為準；每版執行一致性測試，失敗即阻擋發布 |
| registry／表漂移 | CI 欄位檢查失敗 | 阻擋 merge；開 hotfix PR 更新 registry |
| feature mismatch 無法收斂 | ledger open 數不下降 | DS 召集 Model Owner；必要時凍結 deploy spec 變更 |
| trip 關閉語意爭議 | fixture 與業務預期不符 | 回 SSOT 澄清；**不得**在 execution plan 內改定義 |

---

## 11) Working Plan Backlog（上層刻意未決）

以下項目**必須**在 Working plan 另立任務與 owner（本檔只列 backlog）：

| Backlog ID | 項目 | 建議 Owner |
|-------------|------|-------------|
| **BL-01** | 線上 **K/T/D** 最終數值與 SLO | Model Owner + Ops |
| **BL-02** | **L0 不可變儲存**實作選型（追加 vs object 不可變） | Data Platform + Ops |
| **BL-03** | `late_arrival_correction_log` **保留天數／壓縮／GC** 與 L0／published 生命週期對齊 | Ops |
| **BL-04** | **trainer Step 6/7** 與本產線合併／取代／雙軌之時程與回歸範圍 | Model Owner + ML Platform |
| **BL-05** | `cleaned` 單一活躍資料集之保留策略（僅活躍版 + 最近一次回滾點）與分區 GC 週期 | Data Platform + Ops |

---

## 附錄：與 Implementation Plan 章節對照

| Implementation Plan | 本 Execution Plan |
|----------------------|-------------------|
| §5 Phase 0–4 | §4–§8 任務表 |
| §2.3 Resumable 契約 | §5.2–§5.3、E1-09–E1-10、G7 |
| §6.1 / §6.1.1 | E0-05–E0-06、E3-01、E3-06–E3-07 |
| §7.1 | E1-07、G6 |
| §8.1 | §9 Gates（含 G7） |
| §10 correction log | E0-04、E2-06、E4-02、BL-03 |

---

*本檔應隨執行進度更新狀態欄；與 SSOT／Implementation Plan 不一致時，先修正事實再同步三處。*
