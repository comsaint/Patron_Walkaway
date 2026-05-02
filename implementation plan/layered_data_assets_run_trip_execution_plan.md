# 分層資料資產與 run/trip — Execution Plan（Working Plan）

> **文件層級**：Working / Execution Plan（執行層）。  
> **目的**：把 SSOT 與 Implementation Plan 落成**可執行任務**（順序、owner 角色、依賴、產物、DoD、gate、升級規則）。  
> **依據**：[`ssot/layered_data_assets_run_trip_ssot.md`](ssot/layered_data_assets_run_trip_ssot.md)（v1.3）、[`implementation plan/layered_data_assets_run_trip_implementation_plan.md`](implementation%20plan/layered_data_assets_run_trip_implementation_plan.md)（v0.2）、[`schema/time_semantics_registry.yaml`](schema/time_semantics_registry.yaml)、[`package/deploy/models/feature_spec.yaml`](package/deploy/models/feature_spec.yaml)。  
> **邊界**：本檔**不重寫**業務定義與架構決策；若與上層文件衝突，以上層為準並回寫本檔。

---

## 0) 執行摘要與狀態圖例

### 0.1 執行摘要

本輪執行目標為：建立與 `trainer` **並行**之分層資料產線（L0→preprocess→L1→L2→publish→可選 online delta），並以 **manifest、determinism、100% feature 覆蓋、correction log** 作為可驗收交付。Phase 1 **不**產出 trip 最終語義；trip v1 於 Phase 2 一次到位。

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

- SSOT v1.3 可取得且為爭議解方之最高優先序（見 SSOT §0.1）。
- Implementation plan v0.2 可取得（含 Executive Summary、§6.1.1 枚舉規則、§7.1 OOM、§8.1 gate、§10 correction log）。
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

### 2.2 非目標（本輪不強制）

- 不強制本輪完成 **線上 scorer** 讀取 `late_arrival_correction_log`（見 implementation plan §10.2）。
- 不在本輪決定 **K/T/D 最終數值**、**L0 不可變儲存**實作、**trainer Step 6/7 取代與否**（見 §11 backlog）。

---

## 3) 工作分解結構（WBS）總覽

| Phase | 主題 | 關鍵產物 |
|------|------|----------|
| **0** | 契約與 schema freeze | registry 審核流程、preprocess 規格、manifest／correction_log schema、feature dependency registry 初稿 + CI |
| **1** | L1 MVP | L0、`run_fact`、`run_bet_map`、`run_day_bridge`、manifest 預演、Gate 1 + OOM |
| **2** | Trip + published | `trip_fact`、`trip_run_map`、published snapshot、late fixture、ingestion gate |
| **3** | Feature + L2 | asset-layer spec、L2、parity、coverage matrix、mismatch ledger 收斂 |
| **4** | 治理與整合決策 | KPI 儀表、trainer／chunk cache 整合決策包、rollout |

---

## 4) Phase 0 — 契約與 Schema Freeze

### 4.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E0-01** | `time_semantics_registry` PR 流程：template、必填欄位、與 schema dict／FND 對照檢查表 | ML Platform + Data Platform | §1.1 | `.github/` 或 `doc/` 下 PR checklist +（可選）`scripts/validate_time_semantics_registry.py` | 任一改 registry 之 PR 必須觸發檢查；失敗則阻擋 merge |
| ⬜ | **LDA-E0-02** | Preprocessing 規格書：`preprocess_*_v1` 與 FND-01/03/11/13 對照 | DS / Feature Owner + Data Platform | E0-01 | `doc/preprocessing_layered_data_assets_v1.md`（路徑可調，須寫入 repo） | 每條規則有 rule id；與 manifest 可引用欄位對齊 |
| ⬜ | **LDA-E0-03** | Manifest schema：SSOT §8 + `ingestion_delay_summary` | ML Platform | SSOT | `schema/manifest_layered_data_assets.schema.json`（或等價） | JSON Schema 或表格可機器驗證；範例 `manifest.json` 通過驗證 |
| ⬜ | **LDA-E0-04** | `late_arrival_correction_log` schema：對齊 implementation plan §10 + manifest join 鍵 | ML Platform | E0-03 | `schema/late_arrival_correction_log.schema.json` + 範例列 | PK／索引欄位與 §10.1 一致；範例通過驗證 |
| ⬜ | **LDA-E0-05** | Feature enumerator：依 §6.1.1 產出 `features_enumerated.json`（穩定排序） | ML Platform + DS | `feature_spec.yaml` | `artifacts/.../features_enumerated.json` + `scripts/enumerate_deploy_features.py`（或等價） | CI：`enumerated` 列數 = coverage matrix 主鍵數；重跑 deterministic |
| ⬜ | **LDA-E0-06** | Feature dependency registry 初稿：每 `(track_section, feature_id)` 一列 | DS / Feature Owner | E0-05 | `artifacts/.../feature_dependency_registry.csv`（或 yaml） | 欄位含：所需 L1 欄位、是否允許回掃 bet、計算來源占位；無缺列 |
| ⬜ | **LDA-E0-07** | Phase 0 CI gate：registry + manifest + correction_log schema + enumerator | ML Platform | E0-01–E0-06 | CI workflow 或 `make check-layered-contracts` | main／release 分支上該 job 為綠色 |

**Phase 0 完成條件**：E0-01–E0-07 皆 **✅**。

---

## 5) Phase 1 — L1 MVP（無 trip 最終語義）

### 5.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E1-01** | L0 ingest：分區 raw、`source_snapshot_id`、分區 hash 規則 | Data Platform | Phase 0 | L0 目錄結構文件 + 範例批次 | 同一輸入重跑得相同 `source_snapshot_id` 規則文件可重現 |
| ⬜ | **LDA-E1-02** | Preprocess job：輸出清洗後 bet 流／表 + rule id 寫 manifest | Data Platform | E0-02, E1-01 | 清洗後 parquet 或表 + preprocess 版本 tag | manifest 可指涉 `preprocessing_rule_id`／version |
| ⬜ | **LDA-E1-03** | `run_fact` 物化：`run_id` hash 依 implementation plan §4.1（含首筆 `bet_id`） | Data Platform | E1-02 | `run_fact` 分區產物 | Gate 1（§8.1）在 L1 子集通過 |
| ⬜ | **LDA-E1-04** | `run_bet_map` membership | Data Platform | E1-03 | map 產物 | 可由 map 還原每 run 之 bet 集合；與 `run_fact` 一致 |
| ⬜ | **LDA-E1-05** | `run_day_bridge`：跨日 run 影響範圍 | Data Platform | E1-03 | bridge 產物 | 對任意 `gaming_day` 可列出可能受影響之 `run_id` |
| ⬜ | **LDA-E1-06** | Manifest writer：每批次 `manifest.json` + ingestion 摘要（預演） | Data Platform + ML Platform | E0-03, E1-02 | `manifest.json` | 通過 schema；欄位無缺漏 |
| ⬜ | **LDA-E1-07** | OOM runner：實作 §7.1（估算、監控、階梯重試、fail-fast、run log） | Data Platform | implementation plan §7.1 | runner 設定 + 失敗上下文範例 | 故意小 RAM fixture 可觸發降載且輸出仍滿足 determinism 不變式 |
| ⬜ | **LDA-E1-08** | Gate 1 自動化：同 snapshot 多組執行參數 + row hash | ML Platform | E1-03–E1-07 | CI job 或 nightly script | 報告：hash 一致、列數一致、row hash 通過 |

**Phase 1 完成條件**：E1-01–E1-08 皆 **✅**；**不**要求 `trip_fact` 最終語義。

---

## 6) Phase 2 — Trip v1 + Published Snapshot

### 6.1 任務表

| 狀態 | Task ID | 任務 | Owner | 依賴 | 輸出 artifact | DoD |
| :---: | :--- | :--- | :--- | :--- | :--- | :--- |
| ⬜ | **LDA-E2-01** | `trip_fact`：3 個完整 `gaming_day` 關閉語義 | Data Platform | Phase 1 | `trip_fact` | 與 SSOT §3 範例一致之 fixture 測試通過 |
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

**阻塞規則**：任一 Gate 失敗，**禁止**進入下一 Phase 的「對外宣稱完成」狀態；可並行準備下一 Phase 程式，但不得 merge 為 production-ready。

---

## 10) Cadence、風險與升級

### 10.1 Cadence

- **每週**：Phase owner 更新本檔任務表狀態欄（✅／🟡／⏳／⬜）。
- **每次發布 published snapshot 前**：跑 G2–G4 最小檢查套件。
- **每次更動 `feature_spec.yaml` 或 asset-layer spec**：重跑 E0-05 enumerator + coverage diff。

### 10.2 風險與升級（摘要）

| 風險 | 徵兆 | 升級動作 |
|------|------|----------|
| OOM 頻發 | 重試耗盡、單日分區失敗 | Data Platform 降窗／加分桶；記錄峰值；必要時凍結更大窗需求至 Phase 4 |
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

---

## 附錄：與 Implementation Plan 章節對照

| Implementation Plan | 本 Execution Plan |
|----------------------|-------------------|
| §5 Phase 0–4 | §4–§8 任務表 |
| §6.1 / §6.1.1 | E0-05–E0-06、E3-01、E3-06–E3-07 |
| §7.1 | E1-07、G6 |
| §8.1 | §9 Gates |
| §10 correction log | E0-04、E2-06、E4-02、BL-03 |

---

*本檔應隨執行進度更新狀態欄；與 SSOT／Implementation Plan 不一致時，先修正事實再同步三處。*
