# 分層資料資產與 run/trip — Execution Plan（Working Plan）

> **文件層級**：Working / Execution Plan（執行層）。  
> **目的**：把 SSOT 與 Implementation Plan 落成**可執行任務**（順序、owner 角色、依賴、產物、DoD、gate、升級規則）。  
> **依據**：[`ssot/layered_data_assets_run_trip_ssot.md`](ssot/layered_data_assets_run_trip_ssot.md)（**v1.12**）、[`implementation plan/layered_data_assets_run_trip_implementation_plan.md`](implementation%20plan/layered_data_assets_run_trip_implementation_plan.md)（**v0.13**）、[`schema/time_semantics_registry.yaml`](schema/time_semantics_registry.yaml)、[`package/deploy/models/feature_spec.yaml`](package/deploy/models/feature_spec.yaml)、[`trainer/core/_config_training_domain.py`](../trainer/core/_config_training_domain.py)（`GAMING_DAY_START_HOUR`、`HK_TZ`）。  
> **邊界**：本檔**不重寫**業務定義與架構決策；若與上層文件衝突，以上層為準並回寫本檔。

---

## 0) 執行摘要與狀態圖例

### 0.1 執行摘要

本輪執行目標為：建立與 `trainer` **並行**之分層資料產線（L0→preprocess→L1→L2→publish→可選 online delta），並以 **manifest、determinism、100% feature 覆蓋、correction log** 作為可驗收交付。Phase 1 **不**產出 trip 最終語義；trip v1 於 Phase 2 一次到位。

**Phase 1 狀態（精簡；細節見 §5 任務表與 RUNBOOK）**：**LDA-E1-01～E1-11**、**E1-14～E1-16** 均 **✅（MVP）**；合併前跑 **`make check-lda-l0`**。其中 **E1-09／E1-10／E1-11／E1-14～E1-16** 屬 **Phase 1R** 範疇；§5「Phase 1 完成條件」仍以 **E1-01～E1-08** 為門檻敘述。L0 smoke 與 `snap_187e491186316d9a24316f86e06dc6b2` 範例見 `doc/l0_ingest_governance_decisions.md`。

**語義同步註記（2026-05-04 更新）**：治理上層已升版為 **SSOT v1.12**／**Implementation Plan v0.13**。重點收斂：**LDA-016 數值**（7 版／10% 等）**僅**見 implementation plan **§2.2.1**／**§2.4**；SSOT 只保留原則。**Trip close 觀測上界**：以 **`observed_at_logical`** 全域 max（先按 `HK_TZ` 時區正規化）映射 **`G_max`**，再令 **`coverage_end_gaming_day = G_max - 1`**（SSOT §5.1；impl **§4.3**）；現有 `trip_fact_v1.py` 若仍用「全體 `run_end_gaming_day` max」或未做 **`-1`**，**與上層不一致**，待程式對齊。參與上界計算之來源表必須先完成 `observed_at_logical` 量測與 cap 定版。**`GAMING_DAY_START_HOUR`** 以 **`trainer/core/_config_training_domain.py`** 為單一來源；**`run_fact`／`trip_fact` manifest 必須（MUST）**含 **`gaming_day_start_hour_used`**（trip 另含 **`coverage_end_gaming_day`**、`G_max` 與來源表清單）。**`input_hash`**：**內容**指紋（impl **§2.3**）；**應優先**重用上游 manifest **`source_hashes`**，避免重複全檔 `sha256`；編排器若仍混用 file stat 須跟進。**`late_threshold` 與 `ingest_delay_cap_sec`**：可同值但不可隱式綁定；須顯式留痕。**Parity／FND-11** 見 impl **§6.1**（含 canonical 對照提示）。**`late_threshold_status` ∈ {`defined`, `undefined`}`**；僅 `defined` 時輸出 **`late_row_*`**（SSOT §4.4）。**`t_session`**：資產層來源；過渡期暫借 trainer；目標 **`cleaned_session`** 與 mapping 遷入本產線（impl **§2.1**、**§2.4.1**、本檔 **BL-07**）。

**Phase 2 狀態（精簡）**：**LDA-E2-01～E2-04** **🟡（MVP）**；**E2-05～E2-06** **⬜**；**E2-07** **🟡（提案）**。除上表外，尚須：**trip 與 §4.3 上界一致**、**E1-09 編排器接 trip**、**分桶 + single-writer merge**、**無 bet／無 run 等價報告**。
**落差補強（歷史沿用，持續有效）**：one-liner 編排在 raw 模式必須強制走 `t_session -> trainer.identity.build_rated_eligible_player_ids_df -> --eligible-player-ids-parquet`，且對 BET-DQ-03 採 fail-closed（缺 eligible 即失敗，不得以 `preprocessing_gaps` 降級放行）；對應任務見 **E1-14 ~ E1-16**。

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

- SSOT **v1.12** 可取得且為爭議解方之最高優先序（見 SSOT §0.1）。
- Implementation plan **v0.13** 可取得（含 §2.1 `t_session`／遷移、§2.4.1 session 候選、§4.3 trip 上界 **`G_max-1`** 與 **`observed_at_logical`**（含 HK 時區正規化）、§6.1 parity／FND-11 識別提示、§2.3 內容 `input_hash` 與 **I/O 重用**、`GAMING_DAY_START_HOUR` 單一來源、manifest **MUST** 欄位、`late_threshold_status` 枚舉、`late_threshold` vs `ingest_delay_cap_sec` 分工、§6.1.1 枚舉、§7.1 OOM、§8.1 gate、§10 correction log、L0.5 與 **LDA-016** 數值層）。
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
5. **Feature**：deploy `feature_spec.yaml` 依 **§6.1.1** 枚舉之全部 **`(track_section, feature_id)`** 皆覆蓋且與 asset-layer／L2 在 **implementation plan §6.1** 所定義之 **`player_id` 參考語義**下 **deterministic 一致**（**FND-11** 依 §6.1 排除）。
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
| **1R** | L1 resumable + G7/G8 | `materialization_state`、E1-09、E1-10、E1-14~E1-16、日粒度 stop/resume + rated gate |
| **2** | Trip + published | `trip_fact`、`trip_run_map`、published snapshot、late fixture、ingestion gate（**trip 物化：🟡 MVP**；**E2-04 published 指針：🟡 MVP**；**E2-07 K/T/D 提案：🟡**） |
| **3** | Feature + L2 | asset-layer spec、L2、parity、coverage matrix、mismatch ledger 收斂 |
| **4** | 治理與整合決策 | KPI 儀表、trainer／chunk cache 整合決策包、rollout |

---

## 4) Phase 0 — 契約與 Schema Freeze

### 4.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ✅ | **LDA-E0-01** | `time_semantics_registry` PR 流程：template、必填欄位、與 schema dict／FND 對照檢查表 | ML Platform + Data Platform | §1.1 | `.github/` 或 `doc/` 下 PR checklist + `scripts/validate_time_semantics_registry.py` | **DoD（已達）**：`doc/time_semantics_registry_pr_checklist.md`、PR template、`schema/time_semantics_registry.yaml` 含 **`contributes_to_trip_close_horizon`**（v1 僅 `t_bet: true`）；驗證器強制 bool + 貢獻表政策；`python scripts/validate_layered_contracts.py`（等同 `make check-layered-contracts`）會執行該腳本；`.github/workflows/layered_data_assets.yml` 有 workflow。**治理後續**：GitHub branch protection **尚未**將該 CI job 設為 required（團隊已確認）；建議後續補上。 |
| ✅ | **LDA-E0-02** | Preprocessing 規格書：`preprocess_*_v1` 與 FND-01/03/11/13 對照 | DS / Feature Owner + Data Platform | E0-01 | `doc/preprocessing_layered_data_assets_v1.md`（路徑可調，須寫入 repo） | 每條規則有 rule id；與 manifest 可引用欄位對齊 |
| ✅ | **LDA-E0-03** | Manifest schema：SSOT §8 + `ingestion_delay_summary` | ML Platform | SSOT | `schema/manifest_layered_data_assets.schema.json`（或等價） | JSON Schema 或表格可機器驗證；範例 `manifest.json` 通過驗證。**後續 schema 升級（對齊 SSOT v1.12／impl v0.13）**：`run_fact` manifest **MUST** 含 **`gaming_day_start_hour_used`**；`trip_fact` manifest/sidecar **MUST** 含 **`gaming_day_start_hour_used`**、**`coverage_end_gaming_day`**、**`G_max`**（或等價）與 `coverage_input_tables`（或等價來源清單）；`ingestion_delay_summary` **MUST** 含 **`late_threshold_status` ∈ {`defined`, `undefined`}`**；僅 `defined` 時 **`late_row_*` 必填**，`undefined` 時須缺省或 `null`（見 SSOT §4.4）。另需在 schema/文件明示：`late_threshold` 與 `ingest_delay_cap_sec` 不得隱式互綁。 |
| ✅ | **LDA-E0-04** | `late_arrival_correction_log` schema：對齊 implementation plan §10 + manifest join 鍵 | ML Platform | E0-03 | `schema/late_arrival_correction_log.schema.json` + 範例列 | PK／索引欄位與 §10.1 一致；範例通過驗證 |
| ✅ | **LDA-E0-05** | Feature enumerator：依 §6.1.1 產出 `features_enumerated.json`（穩定排序） | ML Platform + DS | `feature_spec.yaml` | `artifacts/.../features_enumerated.json` + `scripts/enumerate_deploy_features.py`（或等價） | `make check-layered-contracts` 內含枚舉與 artifact 一致性 |
| ✅ | **LDA-E0-06** | Feature dependency registry 初稿：每 `(track_section, feature_id)` 一列 | DS / Feature Owner | E0-05 | `artifacts/.../feature_dependency_registry.csv`（或 yaml） | 欄位含：所需 L1 欄位、是否允許回掃 bet、計算來源占位；無缺列（細部 `TBD` 由 DS 後續收斂） |
| ✅ | **LDA-E0-07** | Phase 0 CI gate：registry + manifest + correction_log schema + enumerator | ML Platform | E0-01–E0-06 | CI workflow 或 `make check-layered-contracts` | 本機：`make check-layered-contracts`；遠端 CI 由團隊自設 |

**Phase 0 完成條件**：§4.1 任務表之 **LDA-E0-01–E0-07** 皆 **✅**（含 trip horizon 來源表旗標與驗證器；E0-01 見上列「治理後續」關於 required check）。

---

## 5) Phase 1 — L1 MVP（無 trip 最終語義）

### 5.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ✅ | **LDA-E1-01** | L0 ingest：分區 raw、`source_snapshot_id`、分區 hash 規則 | Data Platform | Phase 0 | 同上 + `scripts/l0_ingest.py` + `layered_data_assets/l0_fingerprint.py` + `schema/examples/snapshot_fingerprint.example.json` + `doc/l0_ingest_governance_decisions.md`；CI：`/.github/workflows/layered_data_assets.yml` | 同一輸入重跑得相同 `source_snapshot_id`；**2026-05-02** 本機真檔 smoke（`baseline_for_baseline_models.parquet` → `snap_187e491186316d9a24316f86e06dc6b2`；見 §0.1 速記） |
| ✅ | **LDA-E1-02** | Preprocess job：輸出清洗後 bet 流／表 + rule id 寫 manifest | Data Platform | E0-02, E1-01 | `scripts/preprocess_bet_v1.py`、`layered_data_assets/preprocess_bet_v1.py`、`layered_data_assets/l1_paths.py`、`schema/examples/manifest_preprocess_bet_l1_example.json` | `cleaned.parquet` + `manifest.json`；`preprocessing_rule_id`=`preprocess_bet_v1`；**歷史 MVP**：dummy／rated sidecar 可選。**v0.6 起編排器路徑由 E1-14~E1-16 升級為 BET-DQ-03 fail-closed**。 |
| ✅ | **LDA-E1-03** | `run_fact` 物化：`run_id` hash 依 implementation plan §4.1（含首筆 `bet_id`） | Data Platform | E1-02 | `layered_data_assets/run_id_v1.py`、`run_fact_v1.py`、`scripts/materialize_run_fact_v1.py`、`l1_paths.l1_run_fact_partition_dir`、`schema/examples/manifest_run_fact_l1_example.json`；主分區 **`run_end_gaming_day`**（SSOT §5.2）；切 run 採 **30 分鐘 gap + `GAMING_DAY_START_HOUR` 硬切**（值來自 `trainer/core/_config_training_domain.py`，非寫死；manifest **必須（MUST）**寫入 `gaming_day_start_hour_used`，對齊 SSOT §8／v1.12）並輸出 `is_hard_cutoff`（或等價欄位） | Gate 1 自動化見 **LDA-E1-08**；本任務 DoD：同輸入 DuckDB `sha256` 與 Python `derive_run_id` 一致（單元測試）、hard cutoff 邊界 fixture 通過、manifest 通過 schema |
| ✅ | **LDA-E1-04** | `run_bet_map` membership | Data Platform | E1-03 | `layered_data_assets/run_bet_map_v1.py`、`scripts/materialize_run_bet_map_v1.py`、`l1_paths.l1_run_bet_map_partition_dir`、`schema/examples/manifest_run_bet_map_l1_example.json`；輸出 `run_bet_map.parquet`（`run_id`, `bet_id`, `player_id`, …）與 manifest | 可由 map 還原每 run 之 bet 集合；與 `run_fact` 之 `bet_count`／首尾 `bet_id` 一致（單元測試） |
| ✅ | **LDA-E1-05** | `run_day_bridge`：日粒度影響分析 | Data Platform | E1-03 | `layered_data_assets/run_day_bridge_v1.py`、`scripts/materialize_run_day_bridge_v1.py`、`l1_paths.l1_run_day_bridge_partition_dir`、`schema/examples/manifest_run_day_bridge_l1_example.json`；輸出 `run_day_bridge.parquet`（`bet_gaming_day` 分區鍵，SSOT §5.2） | 對任意 `bet_gaming_day` 分區可列出該日受影響 `run_id` 集合，供重算範圍掃描（單元測試） |
| ✅ | **LDA-E1-06** | Manifest writer：每批次 `manifest.json` + ingestion 摘要（預演） | Data Platform + ML Platform | E0-03, E1-02 | `layered_data_assets/ingestion_delay_summary_v1.py`、`manifest_lineage_v1.py`；`scripts/preprocess_bet_v1.py` 與 `scripts/materialize_run_*_v1.py` 寫入／合併；`scripts/manifest_lineage_preview_v1.py` 後補 | `make check-lda-l0` 含新單元測試；manifest 仍通過 schema；`source_hashes` 與 fingerprint 銜接見 `doc/l0_ingest_governance_decisions.md` |
| ✅ | **LDA-E1-07** | OOM runner：實作 §7.1（估算、監控、階梯重試、fail-fast、run log） | Data Platform | implementation plan §7.1 | `layered_data_assets/oom_runner_v1.py`；CLI 旗標見 preprocess／`materialize_run_*_v1`；`schema/examples/oom_run_log.example.jsonl`、`oom_failure_context.example.json` | `make check-lda-l0` 含 `test_oom_runner_v1`；mock OOM 重試成功、非 OOM fail-fast；執行參數僅影響資源路徑（G6） |
| ✅ | **LDA-E1-08** | Gate 1 自動化：同 snapshot 多組執行參數 + row hash | ML Platform | E1-03–E1-07 | `layered_data_assets/l1_determinism_gate_v1.py`、`scripts/gate1_l1_determinism_v1.py`、`tests/unit/test_l1_determinism_gate_v1.py` | `make check-lda-l0`：三部 L1 產物在多組 DuckDB 資源設定下列數與 row fingerprint 一致；CLI 可寫 JSON 報告（exit 0/1） |
| ✅ | **LDA-E1-09** | 日粒度 resumable 編排：state store + 原子寫入 + `--resume`/`--force` | Data Platform | E1-02–E1-06 | **`schema/materialization_state.schema.sql`**、**`pipelines/layered_data_assets/orchestration/materialization_state_store_v1.py`**（根目錄 `layered_data_assets/materialization_state_store_v1.py` 為 shim）、**`scripts/lda_l1_gate1_day_range_v1.py`**（`--state-store`／`--resume`／`--force`／`--stop-after-date`；可選 **`--ingestion-fix-registry-yaml`**）、**`pipelines/layered_data_assets/docs/RUNBOOK.md` §5.1**、**`tests/unit/test_materialization_state_store_v1.py`** | **MVP（2026-05-03）**：DuckDB state；`--resume` 同 `input_hash` 則 skip 各步 subprocess；`--force` 重跑；預設 state 路徑見 RUNBOOK；**`--stop-after-date`** 可日中斷演練。**2026-05-03**：`preprocess_bet_v1` 與三 `materialize_run_*_v1` 已對 **`*.parquet`+`manifest.json`** 採 **tmp→`os.replace`**；**LDA-E1-10** G7 見整合測。**v0.6 補強**：one-liner rated gate 見 **E1-14~E1-16**。 |
| ✅ | **LDA-E1-10** | Resume Gate 自動化：中斷/續跑一致性測試 | ML Platform | E1-09 | **`tests/integration/test_lda_e1_10_resume_g7_v1.py`**（G7：`lda_l1_gate1_day_range_v1` 一條龍 vs `--stop-after-date` + `--resume`）；**`cleaned_bet_parquet_row_fingerprint`** in **`pipelines/layered_data_assets/core/l1_determinism_gate_v1.py`**（根 shim：`layered_data_assets/l1_determinism_gate_v1.py`）；`make check-lda-l0` 納入該 integration | **MVP（2026-05-03）**：兩連續日、四 L1 Parquet（preprocess + 三物化）**row_count + row fingerprint** 與 Gate1 既有函式一致；**未**加獨立 kill/SIGINT CI job（可選後補） |
| ✅ | **LDA-E1-11** | Preprocess 升級：接入 `schema/preprocess_ingestion_fix_registry.yaml`，實作 `observed_at_logical`（`t_bet` **`ingest_delay_cap_sec=122`**）、manifest `ingestion_fix_*`／`applied_fix_rules`；`ingestion_delay_summary` 改以 synthetic observed 計算（見 SSOT §4.4 **LDA-014**） | Data Platform | E1-02, E1-06, SSOT v1.12 §4.4 **LDA-014** | **`layered_data_assets/preprocess_bet_ingestion_fix_registry_v1.py`** + 更新 `preprocess_bet_v1.py`、`scripts/preprocess_bet_v1.py`、`tests/unit/test_preprocess_bet_ingestion_fix_registry_v1.py`、`schema/examples/manifest_preprocess_bet_l1_example.json`、`Makefile`（`check-lda-l0` 納入 registry 測試）、**`tests/integration/test_lda_e1_11_gate1_with_registry_v1.py`**（§5.3 列 12） | **不變**：`PARTITION BY bet_id`；輸出主序仍 `payout_complete_dtm, bet_id`；dedup 在傳入 registry 時 `ORDER BY __etl_insert_Dtm_synthetic`；**選用** `--ingestion-fix-registry-yaml`（及可選 `--ingestion-fix-registry-version-expected`）。**MVP 落地 2026-05-03**：`validate_layered_contracts` + `check-lda-l0`；§5.3 **列 12** 由 **`test_lda_e1_11_gate1_with_registry_v1`** 覆蓋（fixture 上帶／不帶 registry 之 L1 四產物 fingerprint 一致 + manifest 含 FIX-004）。 |

**Phase 1 完成條件**：E1-01–E1-08 皆 **✅**；**不**要求 `trip_fact` 最終語義。  
**Phase 1 延伸（例行 smoke／PR）**：**LDA-E1-11** 已 **✅（MVP）**；合併前跑 **`make check-lda-l0`**（已含 **`test_lda_e1_11_gate1_with_registry_v1`**：編排器帶 registry 之一條龍 vs 無 registry 基線）；仍不阻塞 E1-01–E1-08 之已達標敘述。  
**Phase 1R（resumable 擴充）完成條件**：**E1-09**、**E1-10**；**E1-14** **✅**（`test_lda_e1_14_raw_rated_eligible_gate_v1`）；**E1-15** **✅**（`test_lda_e1_15_fail_closed_cutoff_v1` + 編排器 `main`：**raw 且非 dry-run** 且 resolve 後 **eligible=None → exit 2**）；**E1-16** **✅**（`test_lda_e1_16_eligible_canonical_row_budget_v1`：canonical trainer 建檔前 DuckDB `COUNT(*)` 與 `--eligible-build-max-session-rows`；JSONL `canonical_mapping_precount`／`canonical_mapping_build_done`；失敗上下文沿用 `write_failure_context`）。G7 由 **`tests/integration/test_lda_e1_10_resume_g7_v1.py`** 覆蓋（`make check-lda-l0`）；G8 由 E1-14～E1-16 覆蓋 argv、banner 與 CLI fail-closed。可選加強：專用 **SIGINT**／多 worker 併發 CI job。  
**E1-11（ingest cap）驗收建議**：合併主線前跑 **`make check-lda-l0`**（已含 **`test_lda_e1_11_gate1_with_registry_v1`**）；若 `cleaned` 因 dedup tie-break（`__etl_insert_Dtm_synthetic`）導致下游列集合變化，須在 PR／實作說明中列明並以 fixture 或對照表覆蓋預期（列 12 測試假設「synthetic 不影響 dedup 勝者」之 baseline）。

### 5.2 `LDA-E1-09` / `LDA-E1-10` / `LDA-E1-11` / `LDA-E1-14~16` 交付細化

#### `LDA-E1-09`（resumable 編排）最低交付

- state schema 檔：`schema/materialization_state.schema.sql`（或等價）
- runner/CLI：`scripts/lda_l1_day_range_resume_v1.py`（或在既有 `scripts/lda_l1_gate1_day_range_v1.py` 擴充 `--state-store`／`--resume`／`--force`）
- 支援旗標（最低集合）：`--date-from`、`--date-to`、`--resume`、`--force`、`--stop-after-date`（或等價「僅跑 N 日後退出」）、`--state-store`
- **狀態鍵**：至少 `(source_snapshot_id, artifact_kind, partition_day)`；`partition_day` 與各產物 Hive 分區一致（`gaming_day` / `run_end_gaming_day` / `bet_gaming_day` 依 artifact 對照表記錄於 RUNBOOK）
- 原子寫入：`*.tmp` → `rename`（**2026-05-03**：`preprocess_bet_v1` 與三 `materialize_run_*_v1` 已落地；見 `atomic_parquet_manifest_v1.py`）；僅在子程序 exit 0（產物與 manifest 已提交）後編排層才標 `status=succeeded`
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
- **Synthetic observed-at（`t_bet`）**：依 `schema/preprocess_ingestion_fix_registry.yaml` 啟用之規則（例如 **`BET-INGEST-FIX-004`**）產出 `__etl_insert_Dtm_synthetic`；SQL 表達式以 registry／`logical_observed_at_expr` 為準（**不在**本 execution plan 重複貼 SQL，避免漂移）。
- **dedup**：`ROW_NUMBER() … ORDER BY __etl_insert_Dtm_synthetic DESC NULLS LAST, bet_id DESC`（**仍** `PARTITION BY bet_id`）
- **輸出排序**：`ORDER BY payout_complete_dtm ASC NULLS LAST, bet_id ASC`（**不**改為 observed 主序）
- **Manifest**：寫入 `ingestion_fix_rule_id`／`ingestion_fix_rule_version`、`applied_fix_rules`（至少 `BET-INGEST-FIX-004:v1`）、可選 `fix_registry_sha256`；`preprocessing_gaps` 若 registry 缺欄則列明
- **`ingestion_delay_summary`**：`compute_ingestion_delay_summary_preview` 之 `observed_at_col` 改為 `__etl_insert_Dtm_synthetic`（或等價參數化），使摘要反映 cap 後語意
- **驗收**：單元測試覆蓋「delay 超過 cap → synthetic 觸頂」「未超過 → synthetic 等於 raw」；Gate1 在相同輸入下仍通過（run 邊界不依 observed 排序）

#### `LDA-E1-14` ~ `LDA-E1-16`（one-liner rated gate）最低交付

- **單一入口（MUST）**：`scripts/lda_l1_gate1_day_range_v1.py` 在 raw 模式下（`--raw-t-bet-parquet` + `--raw-t-session-parquet`）須自動完成  
  `t_session -> trainer.identity.build_rated_eligible_player_ids_df -> preprocess --eligible-player-ids-parquet`。
- **fail-closed（MUST）**：raw 模式若缺 `raw_t_session` 且未提供可用 eligible 來源，應直接 exit 2；不得用 `preprocessing_gaps` 降級放行 BET-DQ-03。
- **fail-closed（E1-15，2026-05-04）**：**`tests/integration/test_lda_e1_15_fail_closed_cutoff_v1.py`** — raw 無 allowlist、`raw-t-session` 無 cutoff、`--cutoff-dtm` 無效 ISO、`--bet-parquet`+session 無 cutoff／canonical／eligible、缺檔 canonical 且無法建檔等情境皆 **exit 2**；`lda_l1_gate1_day_range_v1.main` 對 **raw 且非 dry-run** 若 resolve 後 **eligible 仍為 None** 再擋一次（避免與 `_validate_mode` 漂移後 silent 進 preprocess）。
- **cutoff 契約（MUST）**：需顯式旗標（例如 `--cutoff-dtm` 或等價設定來源）傳給 `build_rated_eligible_player_ids_df`；不得隱式漂移。
- **整合測（E1-14，2026-05-04）**：**`tests/integration/test_lda_e1_14_raw_rated_eligible_gate_v1.py`** — raw + session + cutoff；`--echo-commands` 斷言 preprocess argv 含 **`--eligible-player-ids-parquet`**；fixture 置於 **`.tmp/lda_e114_*`**（L0 ingest anchor）；**`--canonical-mapping-parquet`** 可指向尚不存在之路徑（resolve 會建）；備份／還原 **`data/canonical_mapping.cutoff.json`**。
- **記憶體/時間約束（MUST）**：eligible 建構需支援欄位裁切與分批/串流，避免全量載入大型 `t_session` 導致 OOM；失敗需輸出可重現錯誤上下文。
- **Trainer 單一來源（MUST）**：LDA 不得重寫 rated 判定 SQL；只能復用 `trainer.identity` 的公共函式語意。

### 5.3 Phase 1R + E1-11 + one-liner rated gate — 子任務拆解與估時（execution checklist）

以下為 **E1-09／E1-10**、**E1-11** 與 **E1-14~E1-16** 落地用工作分解；**估時為單人 person-day 量級**（可並行拆給不同開發者）。實作可優先擴充既有 `scripts/lda_l1_gate1_day_range_v1.py`，或另開 `scripts/lda_l1_day_range_resume_v1.py`（見 §5.2）。

| 狀態 | 序 | 子任務 | Owner | 估時 | 依賴 | 完成定義（DoD） |
| :---: | :---: | :--- | :--- | :---: | :--- | :--- |
| ✅ | 1 | `schema/materialization_state.schema.sql`（或 DuckDB init SQL）落地 + 文件化欄位語意 | ML Platform + Data Platform | 0.5 | E0-03 | **2026-05-03**：`schema/materialization_state.schema.sql`；`ensure_materialization_state_schema` 於編排器啟動時執行 |
| ✅ | 2 | state store 讀寫模組（`pending→running→succeeded|failed`、attempt 遞增） | Data Platform | 1.0 | 1 | **2026-05-03**：`materialization_state_store_v1.py`（`running`／`succeeded`／`failed`、`attempt`）；併發鎖 **未**做（後續可選） |
| 🟡 | 3 | `input_hash` 計算規則固定化：**內容** hash + `source_snapshot_id` + `definition_version` + `transform_version` + 相關 manifest `source_hashes`／registry 版本（**禁止**僅 mtime/size）；**應優先**重用上游已驗證之內容指紋（impl v0.13 §2.3） | Data Platform | 0.5 | E1-02 | **2026-05-03**：曾落地「JSON + sha256（含 stat）」；**2026-05-04**：上層契約改為**內容**為主；**2026-05-04**：補 **I/O 重用**指引（impl v0.13 §2.3）。**待程式對齊**後改回 **✅**；過渡期 PR 須註明是否仍含 stat。 |
| ✅ | 4 | 產物原子寫入包裝（`*.tmp` → rename；失敗不落 `succeeded`） | Data Platform | 0.5 | 2 | **2026-05-03**：`preprocess_bet_v1` 與三 `materialize_run_*_v1` 採 **`pipelines/layered_data_assets/io/atomic_parquet_manifest_v1.py`**（根 shim 仍為 `layered_data_assets/atomic_parquet_manifest_v1.py`）；**編排層**仍為子程序 exit 0 後方標 `succeeded`。Gate1 輸出目錄仍非原子替換（可選後補）。 |
| ✅ | 5 | 編排器／CLI 接上 `--resume` / `--force` / `--stop-after-date` / `--state-store` | Data Platform | 1.5 | 2–4 | **2026-05-03**：`lda_l1_gate1_day_range_v1.py` |
| ✅ | 6 | `pipelines/layered_data_assets/docs/RUNBOOK.md`（或本檔 §5.2）補操作範例與故障排除 | Data Platform | 0.25 | 5 | **2026-05-03**：RUNBOOK §5.1（canonical 路徑；`layered_data_assets/RUNBOOK.md` 為轉址） |
| ✅ | 7 | `LDA-E1-10`：fixture 資料 + kill/resume 測試 +（可選）CI workflow | ML Platform | 1.5 | 5 | **2026-05-03**：`tests/integration/test_lda_e1_10_resume_g7_v1.py`（`--stop-after-date` 代替 SIGINT）；G7 指紋比對 preprocess+三物化；CI workflow **可選** |
| ✅ | 8 | Phase 2 預留：trip 物化日編排**應沿用**同一 state 契約（E2-01 起） | Data Platform | 0.25 | 5 | **2026-05-03**：本檔 §6.1 **LDA-E2-01** 與 implementation plan Phase 2 敘述已註記沿用 **E1-09** state；trip 實作 PR 須引用該契約。 |
| ✅ | 9 | **E1-11**：YAML registry 載入與驗證（path、版本、`ingest_delay_cap_sec`、active rule id） | Data Platform | 0.5 | E1-02 | **2026-05-03**：`preprocess_bet_ingestion_fix_registry_v1.py` + 單元測試；契約不一致／FIX-004 未啟用時 fail-fast |
| ✅ | 10 | **E1-11**：DuckDB SQL 插入 `__etl_insert_Dtm_synthetic` + dedup `ORDER BY` 改為 synthetic | Data Platform | 1.0 | 9 | **2026-05-03**：`preprocess_bet_v1.py`；dedup 僅在傳入 registry 時使用 synthetic；`test_preprocess_bet_v1_ingestion_cap_changes_dedup_winner` |
| ✅ | 11 | **E1-11**：manifest 欄位 + `ingestion_delay_summary` 改 observed 欄位 + 範例 manifest 更新 | Data Platform + ML Platform | 0.75 | 10, E1-06 | **2026-05-03**：manifest 寫入 `ingestion_fix_*`／`applied_fix_rules`；cap 啟用時 summary 用 `__etl_insert_Dtm_synthetic`；範例 JSON 已更新；`validate_layered_contracts` + `check-lda-l0` 通過 |
| ✅ | 12 | **E1-11**：Gate1 迴歸（含 OOM profiles）證明 run 產物不變或僅預期內變更 | ML Platform | 0.5 | 11, E1-08 | **2026-05-03**：**`tests/integration/test_lda_e1_11_gate1_with_registry_v1.py`** — 編排器帶／不帶 registry 在 E1-10 fixture 上 **L1 四產物 row fingerprint 一致**（判準：synthetic observed 不影響 dedup 勝者）；manifest 含 **BET-INGEST-FIX-004**；已納入 **`make check-lda-l0`**（Gate1 仍為編排器內建兩 profile，與 E1-10 一致）。 |
| ⬜ | 13 | **E1-12**：`cleaned` 單一活躍資料集 + rolling 保留策略落地（`gaming_day` 分區 active + 最近 **7** 個成功版本） | Data Platform + Ops | 1.25 | E1-09, E1-11 | 文件化並實作路徑慣例：固定 active root、分區 `*.tmp -> rename` 覆寫、同步更新 state；每分區保留最近 **7** 版且每版具 manifest / `semantic_signature`；不得改變既有業務語義 |
| ⬜ | 14 | **E1-13**：最小追溯與回滾保障（輕量變更索引 + rolling 7 版 GC 與回滾演練） | Data Platform + ML Platform | 1.25 | 13 | 每次分區覆寫都寫事件索引（含 `gaming_day`、`input_hash`、`row_count`、`updated_at`、operator）；完成「第 8 舊版可 GC（審計凍結除外）」與單日回滾演練證據 |
| ✅ | 15 | **E1-14**：編排器 one-liner 接入 trainer rated builder（raw 模式自動建 eligible） | Data Platform | 1.0 | E1-02, E1-09 | **`tests/integration/test_lda_e1_14_raw_rated_eligible_gate_v1.py`**（2026-05-04）：`--raw-t-bet-parquet` + `--raw-t-session-parquet` + `--cutoff-dtm` + `--canonical-mapping-parquet`（repo 下 **`.tmp/lda_e114_*`**，滿足 L0 anchor）；`--echo-commands` 斷言 `preprocess_bet_v1.py` argv 含 `--eligible-player-ids-parquet`；banner 含 `BET-DQ-03 eligible ids`；備份／還原 `data/canonical_mapping.cutoff.json`；`_validate_mode` 允許「缺檔 canonical + 有 session+cutoff」交由 resolve 建檔；已納入 `make check-lda-l0`。 |
| ✅ | 16 | **E1-15**：BET-DQ-03 fail-closed 與 cutoff 旗標契約 | Data Platform + ML Platform | 0.75 | 15 | **`tests/integration/test_lda_e1_15_fail_closed_cutoff_v1.py`**（2026-05-04）：多情境 **exit 2** 與 stderr 關鍵字；**`lda_l1_gate1_day_range_v1.main`** 在 **raw 且非 dry-run** 且 resolve 後 **eligible=None** 時 **exit 2**（補強不得進 preprocess）；已納入 `make check-lda-l0`。 |
| ✅ | 17 | **E1-16**：eligible／canonical trainer 路徑的 session 列數預檢 + run log + 失敗上下文 | Data Platform | 1.0 | 15 | **`tests/integration/test_lda_e1_16_eligible_canonical_row_budget_v1.py`**（2026-05-04）：`--eligible-build-max-session-rows` 在 **補建 canonical** 前以 DuckDB `COUNT(*)` fail-fast（**exit 2**）；`_build_canonical_mapping_parquet_via_trainer` 與 rated-eligible 建檔共用 `--eligible-build-*` 資源／log／failure JSON；已納入 `make check-lda-l0` |

**Phase 1R（E1-09+10）合計（粗估）**：約 **5.5–6.5 person-days**（含測試）；若兩人並行 schema+state 與 CLI，wall-clock 約 **3–4 工作天**。  
**E1-11 加計（粗估）**：約 **2.75–3.5 person-days**（列 9–12）；**列 9–12 已關**（2026-05-03；列 12 見 **`test_lda_e1_11_gate1_with_registry_v1`**）。與 E1-09 並行時 wall-clock 取較長分支 + 合併驗收約 **0.5 天**。  
**E1-12/E1-13 加計（粗估）**：約 **2.25–3.25 person-days**（列 13–14；含 rolling **7** 版保留與 GC/回滾驗證）；若與 E1-11 並行，建議先凍結 active root／索引 schema 再做回滾演練。
**E1-14~E1-16 加計（粗估）**：約 **2.5–3.25 person-days**（列 15–17）；屬 one-liner 可用性與資料品質入口契約，優先序高於 E1-12/E1-13。

**與既有腳本對齊（建議）**

- 首選：在 `scripts/lda_l1_gate1_day_range_v1.py` 外層包一層「按日迴圈 + state」，避免重複維護三套 materialize 呼叫。
- 備選：獨立 `scripts/lda_l1_day_range_resume_v1.py` 僅負責 orchestration，內部仍呼叫既有 preprocess／materialize／gate1。

---

## 6) Phase 2 — Trip v1 + Published Snapshot

### 6.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| 🟡 | **LDA-E2-01** | `trip_fact`：3 個完整 `gaming_day` 關閉語義（實作可用「無 run」等價判定）+ **觀測上界**（SSOT §5.1／impl §4.3：**`observed_at_logical` 全域 max（HK 時區正規化）→ `G_max` → `coverage_end_gaming_day = G_max - 1`**） | Data Platform | Phase 1 + **Phase 1R** | `trip_fact` | **MVP（2026-05-03）**：`materialize_trip_fact_v1` + 分區／manifest／`tests/unit/test_trip_fact_v1.py`；**未滿**：上界（含 **`-1`**、**logical**、HK 時區正規化）與現行程式對齊、空快照 `observed_at_logical` 缺失時 fail-fast、分桶前由單一 coordinator 凍結全域上界並寫 sidecar、等價一致性報告、編排器接軌、分桶+merge 產線級 |
| 🟡 | **LDA-E2-02** | `trip_run_map` membership | Data Platform | E2-01 | `trip_run_map` | **MVP**：同上 CLI 一併產出；**未滿**：獨立 G3 整合測對 published 路徑 |
| 🟡 | **LDA-E2-03** | `trip_id` hash：§4.1 `first_run_id` + `source_snapshot_id` 錨定 | Data Platform | E2-01 | ID 規則單元／整合測試 | **MVP**：`derive_trip_id` + 單元測試；`trip_end_*` 不參與 hash（關閉欄位由邏輯寫入，非事後補寫變更 id 之流程） |
| 🟡 | **LDA-E2-04** | Publisher：`published_snapshot_id`、sidecar manifest、回滾策略 | Data Platform + Ops | E0-03, E2-01 | `published_snapshot.json` + 目錄慣例文件 | **MVP（2026-05-03）**：JSON Schema、範例、`publish_layered_snapshot_v1` CLI、`l1_layered/published/snapshots/<pub>/` + `current.json`、`make check-lda-l0` 單測；**未滿「✅」**：與 L1 manifest 併行之 sidecar、正式回滾／審計流程文件化 |
| ⬜ | **LDA-E2-05** | Published ingestion：`ingestion_delay_summary` 強制 | Data Platform | E2-04 | published 批次 manifest | **缺失率 = 0** |
| ⬜ | **LDA-E2-06** | `late_arrival_correction_log` writer + fixture | Data Platform | E0-04, E2-04 | correction log 範例 + SSOT 對齊測試 | late bet／correction fixture 下 log 與 ID 變化符合預期 |
| 🟡 | **LDA-E2-07** | K/T/D 提案文件：數值建議 + 負載評估（不定最終值） | DS + Data Platform | SSOT §5.4 | **`doc/ktd_proposal_layered_data_assets.md`**（2026-05-03） | **提案稿**：三層候選（K/T/D）、上界公式、活躍玩家變數 A、簽核表；**滿「✅」尚缺**：簽核 + 真實延遲分佈校準後更新文件與狀態 |

**Phase 2 完成條件**：E2-01–E2-07 皆 **✅**；Gate 3–4（membership、ingestion）對 published 路徑成立。

### 6.2 Phase 2 v1 定案（2026-05-03）

- **分區鍵**：`trip_fact` 固定以 `trip_start_gaming_day` 分區。
- **輸出粒度**：`trip_fact` 必須同時包含已關閉與進行中 trip。
- **計算邊界**：Trip close 判定 v1 不引入外部賭場日曆表；以 `run_fact` 有 run 日與缺口推導（缺資料日視為完整空日）；**並**須先凍結全域 **`coverage_end_gaming_day`**（**`observed_at_logical` max（HK 時區正規化）→ `G_max` → `G_max - 1`**；見 impl §4.3），**禁止**以其他玩家之最後活動日替代上界。
- **執行模式（MVP）**：先做 full snapshot 重算；按日增量 trip 重算列為 Phase 2 後續（接 E1-09 state）。
- **寫入策略**：多 worker 僅寫暫存，最終由 single writer 固定排序合併分區（determinism）。
- **Lineage**：`trip_fact` manifest `source_partitions` 必須列舉觸及之所有 `run_end_gaming_day` 分區，`source_hashes` 與其對齊。

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
| **G5 Feature** | §6.1.1 全量 `(track_section, feature_id)` 覆蓋 + 於 **§6.1 `player_id` 參考語義**下 deterministic 一致（FND-11 排除） | Phase 3 |
| **G6 OOM Invariant** | 執行參數僅影響資源／時間，不影響語義輸出 | Phase 1 起與每次大表變更 |
| **G7 Resume Invariant** | 一次跑完 vs 中斷續跑結果一致；成功分區可安全 skip | Phase 1 起（E1-09 完成後） |
| **G8 Rated Gate Invariant** | raw one-liner 必須包含 BET-DQ-03（trainer identity 來源）且 fail-closed；缺 eligible 不得放行 | Phase 1R（E1-14~E1-16） |

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
| `GAMING_DAY_START_HOUR` 與來源 `gaming_day` 口徑漂移 | run 邊界異常抖動、`is_hard_cutoff` 比例異常 | 單一來源為 `trainer/core/_config_training_domain.py`；manifest 記錄實際值；CI 鎖定「物化值 = trainer import 值」；變更視為 `definition_version` 事件並回寫上層文件 |
| resumable state 損毀或不一致 | 成功分區被覆寫、失敗分區被誤跳過 | 啟用 state/manifest 雙重校驗；原子寫入；`--force` 僅允許顯式重算 |
| trip「無 bet」與「無 run」判定漂移 | 關閉時點偏移，影響 trip_id 與特徵 | 維持語義以「無 bet」為準；每版執行一致性測試，失敗即阻擋發布 |
| registry／表漂移 | CI 欄位檢查失敗 | 阻擋 merge；開 hotfix PR 更新 registry |
| feature mismatch 無法收斂 | ledger open 數不下降 | DS 召集 Model Owner；必要時凍結 deploy spec 變更 |
| trip 關閉語意爭議 | fixture 與業務預期不符 | 回 SSOT 澄清；**不得**在 execution plan 內改定義 |
| 下游誤用 `trip_id`／`run_id` 跨 snapshot | 報表 join 靜默錯位 | 消費端 checklist：併 `source_snapshot_id`；見 SSOT §6、impl plan §7 |

---

## 11) Working Plan Backlog（上層刻意未決）

以下項目**必須**在 Working plan 另立任務與 owner（本檔只列 backlog）：

| Backlog ID | 項目 | 建議 Owner |
|-------------|------|-------------|
| **BL-01** | 線上 **K/T/D** 最終數值與 SLO | Model Owner + Ops |
| **BL-02** | **L0 不可變儲存**實作選型（追加 vs object 不可變） | Data Platform + Ops |
| **BL-03** | `late_arrival_correction_log` **保留天數／壓縮／GC** 與 L0／published 生命週期對齊 | Ops |
| **BL-04** | **trainer Step 6/7** 與本產線合併／取代／雙軌之時程與回歸範圍 | Model Owner + ML Platform |
| **BL-05** | `cleaned` 單一活躍資料集之保留策略（active + 每分區 rolling 最近 **7** 版 + 回滾點）與分區 GC 週期 | Data Platform + Ops |
| **BL-09** | `time_semantics_registry` 新增並治理 **trip horizon 來源表旗標**（例如 `contributes_to_trip_close_horizon`）：未聲明表預設不得參與 `coverage_end_gaming_day` | ML Platform + Data Platform |
| **BL-06** | **Future entity onboarding template**（`cleaned_<entity>` 接入前最小契約：`entity_name`、`business_key`、`partition_key_semantics`、`event_time_col`、`observed_at_col`；impact scope 與 fallback policy；可先 `TBD` 但 production 前需定版） | Data Platform + ML Platform + DS |
| **BL-07** | **`t_session` → `cleaned_session`（L0.5）** 與 **canonical／rated mapping** 由 trainer 遷入本產線：與 `cleaned_bet` 同等 manifest／state／impact；過渡期文件與 CI 標註 trainer 為單一來源 | Data Platform + ML Platform |
| **BL-08** | **`GAMING_DAY_START_HOUR` / `HK_TZ` 契約鎖定**：LDA 物化必須直接讀取 `trainer/core/_config_training_domain.py`，並在 CI 檢查 manifest `gaming_day_start_hour_used` 與 runtime import 值一致 | ML Platform + Data Platform |

### 11.1 Future entity onboarding（best-effort 模板，不排期）

> 目的：在未知未來表清單下，先固定接入 gate，避免新實體繞過治理。

| 檢核項 | 要求 | 備註 |
|------|------|------|
| 最小契約欄位 | 必填五欄：`entity_name`、`business_key`、`partition_key_semantics`、`event_time_col`、`observed_at_col` | 前期可 `TBD`，production 前必須定版 |
| 分區鍵策略 | entity-specific；不得預設一律 `gaming_day` | 與 SSOT v1.12 / impl v0.13 對齊 |
| 影響分析 | 需在 `impact_analyzer` 註冊 `entity_name` 與 `impact_scope` | 無 machine-readable scope 時不得走增量發布 |
| Fallback policy | 需有實體級規則（比例／絕對量／扇出成本） | bet 的 10% 只能當暫時參考，不可直接複製 |
| Lineage / state | 必須接入 manifest、`semantic_signature`、state store | 未接入者視為實驗管線，不得宣稱 production-ready |

---

## 附錄：與 Implementation Plan 章節對照

| Implementation Plan | 本 Execution Plan |
|----------------------|-------------------|
| §5 Phase 0–4 | §4–§8 任務表 |
| §2.2.1 統一失效模型（含 bet 路徑 `impact_day_ratio`／`changed_player_ratio` 與 full recompute 備援；**LDA-016 數值僅在本層**） | E4-01（KPI／觀測）、§10.2；implementation plan §8.1（invalidation gate）；SSOT **LDA-016** 僅原則 |
| §2.4／§2.4.1 L0.5 `cleaned_bet`（rolling **7**）與多實體掛載預留；§2.1 `t_session`／遷移 | BL-05、BL-06、**BL-07**、§11.1；E1-12／E1-13 backlog |
| §2.2 Trip horizon 來源表旗標（`contributes_to_trip_close_horizon`） | **LDA-E0-01** ✅、**BL-09** |
| §4.2 / §4.3 `GAMING_DAY_START_HOUR` / `HK_TZ` 單一來源與 trip 上界時區契約 | §10.2 風險列、**BL-08**、E2-01 DoD（時區正規化 + 單點凍結上界） |
| §2.3 Resumable 契約 | §5.2–§5.3、E1-09–E1-10、G7 |
| one-liner + BET-DQ-03 fail-closed（自 implementation plan v0.6 起） | §5.2（E1-14~E1-16）、§5.3 列 15–17、G8 |
| §6.1 / §6.1.1 | E0-05–E0-06、E3-01、E3-06–E3-07 |
| §7.1 | E1-07、G6 |
| §8.1 | §9 Gates（含 G7） |
| §10 correction log | E0-04、E2-06、E4-02、BL-03 |

---

*本檔應隨執行進度更新狀態欄；與 SSOT／Implementation Plan 不一致時，先修正事實再同步三處。*
