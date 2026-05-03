# 分層資料資產與 run/trip 特徵工程 — Implementation Plan

> **版本**：Implementation plan **v0.5**（2026-05-03；同日補述：`preprocess_bet_v1` 去重／輸出序邊界與 execution plan 對齊）。對齊 SSOT v1.5；重大架構變更升 minor。  
> **依據**：`ssot/layered_data_assets_run_trip_ssot.md`（v1.5）、`schema/time_semantics_registry.yaml`。  
> **本文層級**：架構、模組邊界、階段交付、驗證與治理；**不含**逐檔 Jira 式任務拆解。  
> **與 trainer 關係**：本計畫先建立**與現行 `trainer` 管線並行**之資料資產產線；是否改為訓練主讀本層資產須另案決策（見 SSOT §0.1）。

---

## 執行摘要 (Executive Summary)

**為何需要此計畫？**  
目前 Walkaway 專案的特徵計算多集中在 `trainer` 管線的後段（如 Step 6/7），且高度依賴 `canonical_id` 與記憶體內的 chunk cache。這導致兩個痛點：
1. **重算成本極高**：訓練窗或策略參數微調，常觸發跨大表的全量歷史重掃。
2. **特徵工程受限**：缺乏獨立的 `run` 與 `trip` 聚合層，難以低成本開發跨 run/trip 的高階特徵。

**本計畫的解決方案**  
依據 SSOT 規範，建立一條**獨立於現有 trainer** 的「分層資料資產產線」：
- **L0 (Raw)** → **L1 (Reusable Facts)**：將最昂貴的序列彙總，物化為穩定的 `run_fact` 與 `trip_fact`，並確保 100% Determinism。
- **L2 (Assembly)**：在輕量組裝層才套用訓練所需的抽樣與權重。
- **解耦特徵定義**：建立 asset-layer `feature_spec`，以 `player_id` 為唯一玩家鍵，並強制要求 100% 覆蓋現有 deploy spec 的所有特徵。

**預期成果**  
完成本計畫後，專案將具備：
1. **可重用、可增量的離線特徵庫**（支援每日 published snapshot）。
2. **資源可控的計算管線**（具備 OOM 估算與自動降載重試機制）。
3. **嚴格的審計與驗收標準**（Lineage manifest、100% Feature Parity、以及處理遲到資料的 Correction Log）。
4. 大幅降低未來模型迭代與特徵開發的 Time-to-Ready。

---

## 0) 設計原則

1. **SSOT 優先**：行為與契約以 `layered_data_assets_run_trip_ssot.md` 為準；本計畫不擴寫業務定義。
2. **分層分責**：L0（raw 快照）→ 專用 preprocessing → L1（run/trip facts + membership + bridge）→ L2（組裝／訓練消費）；**訓練策略參數不進 L1 失效鍵**。
3. **可重現與可審計**：每一批次產物可指涉 `source_snapshot_id`、`definition_version` / `feature_version` / `transform_version`、manifest、必要時 **ingestion 延遲摘要**。
4. **資源可控**：大表掃描以 **gaming_day（或約定分區）** 增量為主；單機路徑須支援 **串流／分批**（DuckDB 或等價），避免假設可一次載入全歷史。
5. **與既有特徵契約對齊**：以 `package/deploy/models/feature_spec.yaml` 為覆蓋底線；採 **`player_id` + asset-layer `feature_spec`**；**全量枚舉、100% 重現與驗收**之操作定義見 **§6.1**（含 **§6.1.1**）。deploy 檔內之 `canonical_id`／`PARTITION BY canonical_id` 等僅為**現況描述**；本產線不採 `canonical_id` 作為特徵主分區鍵（見 §6.1）。
6. **可中斷與可續跑（resumable）**：以 `gaming_day` 為最小計算單元；任何單日產物需可獨立重跑、跳過已完成分區、並在中斷後從未完成日期續跑。

---

## 1) 目標狀態（As-Is → To-Be）

### 1.1 As-Is（摘要）

- 重計算多集中在既有 `trainer` Step 6/7 等路徑；chunk cache 與多種訓練參數可能使快取鍵過寬、易觸發全量重算。
- 特徵語意部分依賴 `canonical_id` 與既有 SQL／函式（如 `feature_spec.yaml` 內之 `PARTITION BY canonical_id`）。

### 1.2 To-Be（本計畫完成後應具備）

- 獨立產線可產出並版本化：**`run_fact`、`trip_fact`、`run_day_bridge`、`run_bet_map`、`trip_run_map`**（名稱可實作時調整，語義須符合 SSOT）。
- **`schema/time_semantics_registry.yaml`** 為納入 L1 之來源表前置條件；變更須審核。
- **離線 published snapshot**（至少支援每日一版）+ **線上有界增量** 之消費契約可驗收（K/T/D 於 Phase 2 定量）。
- **Manifest** 符合 SSOT §8，並含 **ingestion 延遲摘要**（對 published 批次為強制驗收項）。
- **Feature parity**：見 **§6.1**／**§6.1.1**（L1/L2 全量重現 deploy `feature_spec.yaml` 之可枚舉特徵條目）。

---

## 2) 架構總覽

### 2.1 管線階段

| 階段 | 輸入 | 輸出 | 備註 |
|------|------|------|------|
| **L0 ingest** | ClickHouse / 既有 Parquet 匯出 | 分區 raw（immutable batch） | 每批有 `source_snapshot_id`、分區 hash |
| **Preprocess** | L0 | 清洗後 bet 流（或中間表） | 與 `dedup_rule_id`、registry 對齊 |
| **L1 materialize** | 清洗後 bets | `run_fact`、`trip_fact`、`run_day_bridge`、membership maps | 依 `definition_version`；**不含**訓練抽樣參數 |
| **L2 assemble** | L1 + 需求窗 | 訓練或分析用矩陣／索引 | 抽樣、權重僅在此層 |
| **Publish（serving 基底）** | L1/L2 | `published_snapshot_id` + sidecar manifest | 週期與 SLO 於 Phase 2 固定 |
| **Online delta（可選）** | 新 bet 流 + 上一版 published | bounded state + `late_arrival_correction_log` | 須可證明上界 |
| **Resumable orchestration** | 日期區間 + 版本鍵 + 既有狀態 | 單日計算狀態（pending/running/succeeded/failed/skipped）+ 可續跑計畫 | 以 `gaming_day` 為最小單元，支援 stop/resume |

### 2.2 儲存與編排（建議）

- **預設**：分區 **Parquet** + **DuckDB** 掃描／增量 SQL（與專案現況一致）。
- **Registry**：`schema/time_semantics_registry.yaml`（單一真相）；CI 或 PR checklist 驗證 schema 與欄位存在性。
- **Manifest**：每批次目錄 `manifest.json`（或集中 registry DB）；published 另寫 `published_snapshot.json`。
- **編排**：初期以 **CLI + cron/Airflow（若已有）** 即可；不將編排器選型列為本計畫 gate。
- **State store（新增）**：維護日粒度執行狀態（例如 SQLite/DuckDB/JSONL）；每筆至少含 `artifact_kind`、`gaming_day`、`source_snapshot_id`、`definition_version`、`transform_version`、`status`、`attempt`、`input_hash`、`output_uri`、`updated_at`、`error_summary`。

### 2.3 Resumable 工程契約（草案）

**最小資料表（建議名：`materialization_state`）**

| 欄位 | 型別（建議） | 說明 |
|------|------|------|
| `artifact_kind` | TEXT | `preprocess_bet` / `run_fact` / `run_bet_map` / `run_day_bridge` / `trip_fact` / `trip_run_map` |
| `gaming_day` | DATE/TEXT | 單日單元鍵（`YYYY-MM-DD`） |
| `source_snapshot_id` | TEXT | 輸入快照版本 |
| `definition_version` | TEXT | run/trip 定義版本 |
| `transform_version` | TEXT | 流程版本 |
| `input_hash` | TEXT | 該日輸入內容指紋（用於 skip/stale 判定） |
| `status` | TEXT | `pending` / `running` / `succeeded` / `failed` / `skipped` |
| `attempt` | INTEGER | 該單元嘗試次數 |
| `output_uri` | TEXT | 成功輸出檔 URI（可空） |
| `row_count` | BIGINT | 成功輸出列數（可空） |
| `row_hash` | TEXT | row-level hash/checksum（可空） |
| `error_summary` | TEXT | 失敗摘要（可空） |
| `updated_at` | TIMESTAMP | 最後更新時間 |

**唯一鍵（建議）**

- `UNIQUE (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)`

**狀態轉移（MUST）**

- `pending -> running -> succeeded|failed`
- `succeeded -> skipped`（僅在 `--resume` 且輸入/版本未變時）
- 任一狀態 `-> running`（僅在 `--force` 顯式重算）

**CLI 契約（最小）**

- `--date-from YYYY-MM-DD` / `--date-to YYYY-MM-DD`
- `--resume`：僅跑 `pending|failed`，`succeeded` 預設 skip
- `--force`：忽略既有 `succeeded`，強制重算指定日期
- `--stop-after-date YYYY-MM-DD`：驗證可中斷點
- `--state-store PATH`：指定 state DB（預設 `data/l1_layered/materialization_state.duckdb`）

**原子寫入（MUST）**

- 目標檔先寫 `*.tmp`，完成校驗（row_count/hash）後 rename 成正式檔。
- 僅在 rename 成功後寫入 `status=succeeded`；任何中斷都不得留下「成功狀態 + 半成品檔」。

---

## 3) 模組邊界與職責

| 模組 | 職責 |
|------|------|
| **time_semantics_registry** | 維護每表 `event_time` / `observed_at` / `business_key` / `dedup_rule_id`；與 `GDP_GMWDS_Raw_Schema_Dictionary.md`、FND 對齊。 |
| **preprocessing** | `player_id` / `bet_id` 有效性、重複版本、`manual/canceled/deleted` 等；輸出 rule id 與版本供 manifest。 |
| **run_trip_builder** | 依 SSOT run v2 切 run（30 分鐘 gap + `gaming_day` hard cutoff）、依 3 個完整 gaming_day 關 trip；產出 facts + membership + bridge。 |
| **lineage_manifest_writer** | 寫 SSOT §8 欄位 + `ingestion_delay_summary`（published 強制）。 |
| **feature_dependency_registry**（新建或併入 doc） | 依 **§6.1.1** 自 deploy `feature_spec.yaml` 枚舉 `(track_section, feature_id)` → 所需 L1 欄位／是否允許回掃 bet。 |
| **parity_validator** | 抽樣或窗內比對：**依 deploy `feature_spec.yaml` 獨立重算之參考值** vs **asset-layer / L2 產出**（見 §6）；不依賴既有 trainer 快取產物作為唯一真相。 |
| **publisher** | 產出 `published_snapshot_id`、刷新週期標識、可選 `online_delta_seq` 契約。 |
| **resume_controller** | 依 state store + manifest 決定「可跳過／需重跑／可續跑」日期集合；提供 `--resume` / `--force` / `--date-from` / `--date-to` 契約。 |

**與 `trainer` 邊界**：第一階段 **不重構** `trainer.py`；僅定義「可讀取 L1 產物」之介面契約，供後續合併決策。

---

## 4) 主鍵與排序（實作約定）

### 4.1 `run_id` / `trip_id`（snapshot-scoped deterministic）

- 遵循 SSOT §6：**同一 `source_snapshot_id` + 相同輸入與版本**下重跑必得相同 ID。
- **實作補強（建議寫入本計畫之工程契約）**：`run_id` 之 hash 輸入除 `(player_id, run_start_ts, run_definition_version, source_namespace)` 外，**納入該 run 之第一筆 `bet_id`（依 `payout_complete_dtm ASC, bet_id ASC`）**，以避免同 timestamp 精度下之邊界歧義與碰撞風險。
- **`trip_id`（工程契約）**：hash 輸入至少包含 `(player_id, trip_start_gaming_day, trip_definition_version, source_namespace, first_run_id)`。其中 **`first_run_id`** 為該 trip 內依 **`run_start_ts ASC, run_id ASC`** 排序後之**第一個** `run_id`（`run_start_ts` 為 `run_fact` 與實作對齊之 run 起始事件時間欄位；`run_id` 本身已定義為 snapshot-scoped deterministic，見上條）。

### 4.2 事件序與 PIT

- Bet 序：**`ORDER BY payout_complete_dtm ASC, bet_id ASC`**（與 trainer SSOT C1 精神一致）。
- Run 邊界：同時套用 **`gap <= 30 分鐘`** 與 **`gaming_day` 變更硬切**；`gaming_day` 邊界由 **`GAMING_DAY_START_HOUR`** 決定（目前專案設定為 `3`，Asia/Hong_Kong）。
- 邊界欄位：`run_fact` 應輸出 **`is_hard_cutoff`**（或等價 `boundary_reason`），供訓練排除與審計追溯。
- 版本治理：`GAMING_DAY_START_HOUR` 或 hard cutoff 規則調整，必須升 `run_definition_version` 並觸發受影響分區重算。
- 任何「以 observed_at 取代 event_time 排序」之行為 **禁止**（SSOT §4.4）。
- **`observed_at_logical` 契約（SSOT v1.5 / LDA-014）**：對來源表先排除已文件化整批入倉 episode，量測 `observed_at_raw - event_time` 殘差 P95，登錄 `ingest_delay_cap_sec`；preprocess 階段以 `min(observed_at_raw, event_time + cap)` 產生邏輯可觀測時間（例如 `__etl_insert_Dtm_synthetic`）。`L0` raw 時戳不得覆寫。
- **`preprocess_bet_v1` 實作邊界（2026-05-03 定案）**：`bet_id` 去重仍 **`PARTITION BY bet_id`**（**不**改為 `PARTITION BY gaming_day, bet_id`）；`cleaned` 輸出主序仍 **`ORDER BY payout_complete_dtm ASC, bet_id ASC`**（與本節 Bet 序、`run_fact_v1`、scorer 穩定排序一致）。`observed_at_logical` 僅用於 ingest-delay 分析／manifest 摘要與（若實作）dedup tie-break，**不得**取代 `payout_complete_dtm` 作為業務事件序主鍵。

### 4.3 Trip close 計算等價式（實作契約）

- **語義不變（MUST）**：trip 仍採 SSOT 定義「**3 個完整 `gaming_day` 無 bet 才關**」。
- **計算替代（SHOULD）**：在 run hard cutoff 已啟用前提下，可用「**3 個完整 `gaming_day` 無 run**」作為實作判定，以降低計算成本。
- **等價前提（MUST）**：同一資料範圍內，必須成立 `有 bet <=> 該日存在至少一個 run`（run 由 bet 壓縮而來，不允許空 run）。
- **驗證要求（MUST）**：每次 `definition_version` 升版或邊界規則調整時，需以 fixture/抽樣驗證「無 bet」與「無 run」判定結果一致。

---

## 5) 分階段交付（Phases）

### Phase 0 — 地基與契約（約 1–2 週，視人力）

**目標**：可空跑、可審核、無產物歧義。

**交付物**：

- `schema/time_semantics_registry.yaml` 之 **審核流程**（對應 SSOT §11 議題 7）：PR template、必填欄位檢查。
- **Preprocessing 規格書**（短文件即可）：對應 `preprocess_*_v1` 與 FND-01/03/11/13 之對照表。
- **Ingestion fix registry**：`schema/preprocess_bet_ingestion_fix_registry.yaml`（及後續表別 registry）欄位契約、版本策略、與 `time_semantics_registry` 一致性檢查；需包含 bulk episode 證據與 `ingest_delay_cap_sec` 量測方法。
- **Manifest schema**（JSON schema 或表格）：欄位含 SSOT §8 + `ingestion_delay_summary` 結構約定。
- **`late_arrival_correction_log` schema**（JSON schema 或表格）：與 §10 最小欄位契約一致，與 manifest 可追溯 join 鍵一併鎖定。
- **Feature dependency registry** 初稿：依 **§6.1.1** 自 `feature_spec.yaml` 列出每一 `(track_section, feature_id)` 所需欄位與 partition key。

**里程碑**：registry + manifest + correction_log schema 可被 CI 驗證（例如欄位存在、版本號格式）。

### Phase 1 — L1 最小可用（MVP）（約 2–4 週）

**目標**：單一 `gaming_day`（或小窗）可端到端產出 L1。

**交付物**：

- L0 分區寫入與 `source_snapshot_id` 產生規則。
- Preprocess → `run_fact`、`run_bet_map`、`run_day_bridge`（run 依 `gaming_day` hard cutoff 切分，`run_fact` 含 `is_hard_cutoff` 或等價欄位；Phase 1 不產出 trip 最終語義；trip 於 Phase 2 一次到位導入 v1 規則）。
- 每批次 manifest + **ingestion 延遲摘要**（published 路徑預演）。
- 日區間編排支援 **resumable**：每個 `gaming_day` 單獨落檔並寫入 state store；中斷後可從未完成日期續跑。

**驗證**：

- **Determinism**：依 **§8.1 Gate 1** 全文（同 snapshot、不同 §7.1 執行參數組合下 **hash 與列數**一致，以及約定關鍵欄位之 **row-level canonical hash**／checksum 抽檢或全量）。
- 記憶體與 OOM：**§7.1**（估算、監控、降載重試、fail-fast）；不得為通過驗收而放寬 L1 業務語義。

### Phase 2 — Trip 與 published snapshot（約 2–4 週）

**目標**：trip 依 **3 個完整 gaming_day** 關閉；每日 published。

**交付物**：

- `trip_fact`、`trip_run_map`；與 `run_fact` 分區策略（`run_end_gaming_day`，以及可選 `run_day_bridge` 影響分析）對齊。
- `published_snapshot_id` 發布流程與 **回滾策略**（保留上一版 snapshot 指標）。
- **K/T/D** 線上有界緩衝之數值建議與負載評估（對應 SSOT §5.4）。
- trip close 採「**語義維持無 bet**、實作可用無 run 等價判定」並輸出一致性驗證報告。

**驗證**：

- 人工構造 late bet / correction fixture：**snapshot-scoped ID 變化**與 `late_arrival_correction_log` 行為符合 SSOT。
- Ingestion summary：**published 批次缺失率 = 0**（對應 SSOT §9 KPI）。

### Phase 3 — Feature coverage 與 L2（約 3–6 週，與特徵複雜度綁定）

**目標**：L2 可組裝出與 `feature_spec.yaml` 對齊之特徵集（在 `player_id` 契約下）。

**交付物**：

- `run_fact` 欄位最小集合（由 feature dependency registry 驅動）。
- 必要時 **trip 級聚合特徵** 與例外「回掃 bet」清單（須 registry 記錄原因）。
- **Parity 報告**：deploy spec 獨立重算 vs asset-layer／L2 之差異與收斂狀態（見 §6）。

**驗證**：

- 抽樣窗內：特徵值與關鍵序關係需 deterministic 一致；任一不一致需歸因並修復，不以放寬門檻結案。

### Phase 4 — 整合與治理（持續）

**目標**：重用率、重算率、Time-to-ready 可觀測。

**交付物**：

- 儀表或週報：Reuse rate、Recompute ratio、Time-to-ready(p95)、ingestion coverage。
- **與 chunk cache / Step 6** 之整合決策文件（對應 SSOT §11 議題 4）：合併、取代或雙軌並行。

---

## 6) Feature spec 對齊與驗證策略

### 6.1 契約

- **底線**：`package/deploy/models/feature_spec.yaml` 所列特徵須可由本產線重建（SSOT LDA-013）；**可枚舉集合與覆蓋率**以 **§6.1.1** 為準。
- **覆蓋與重現（硬性）**：§6.1.1 所定義之**每一條**可枚舉特徵皆須納入 registry／coverage／驗證；**不論** track 或條目層級之 `enabled` / `disabled` 等狀態，**皆須可重現**（asset-layer 可標註「僅供審計／不進線上模型」但不得缺項）。**覆蓋率必須 100%**，不允許「先部分覆蓋」作為最終驗收。
- **粒度決策（定版）**：採 **B**。維持 `player_id`，並建立/維護 **asset-layer `feature_spec`**（與 deploy 包解耦）；不得在本計畫內引入 `canonical_id` 作為特徵主分區鍵。
- **一致性標準（硬性）**：採 deterministic from-scratch 計算，預設不設 `abs_diff`/`rel_diff` 容忍門檻。若遇到浮點非結合律導致差異，必須先修正計算序與聚合順序（例如固定 reduce order），而非放寬驗收門檻。

#### 6.1.1 可枚舉特徵條目（操作定義）

以下為 **CI／coverage matrix** 與 **parity_validator** 共用之枚舉規則，避免人工判斷「算不算一條 feature」：

- **來源檔**：`package/deploy/models/feature_spec.yaml`（以 repo 中該路徑之已解析 YAML AST 為準；**不**以註解或游離文字列計）。
- **枚舉範圍**：對檔內每一頂層鍵名符合 **`track_*`** 且其值為 mapping、並含 **`candidates:`** 清單之區塊（目前為 `track_llm`、`track_human`、`track_profile`；若未來新增 `track_foo` 亦自動納入），遍歷該 **`candidates`** 清單中**每一個** list element。
- **計入條件**：該 element 為 mapping 且含 **`feature_id`** 鍵者，計為**一條**可枚舉特徵。
- **穩定識別鍵**：以 **`(track_section, feature_id)`** 為唯一鍵，例如 `("track_llm", "bets_cnt_w5m")`；`coverage matrix`／`mismatch ledger`／registry 皆須使用此複合鍵，避免不同 track 下同名衝突。
- **狀態**：枚舉時**不**依 `tracks_enabled`、`track_*.enabled`、或任何其他開關過濾；凡符合上列結構者**一律**納入 100% 覆蓋與 deterministic 驗收。
- **非特徵條目**：`execution`、`guardrails`、`inference_state` 等設定區塊**不**計入「特徵條目」枚舉；其語意由 asset-layer spec 另行對齊 deploy 引擎行為時再文件化。

### 6.2 Parity 方法（建議）

- **序特徵**：同一 `player_id`、同一時間窗內，比對 `prev_bet_gap_min`、`loss_streak`、run boundary 類等之排序一致性。
- **聚合窗特徵**：比對 `bets_cnt_w*`、`wager_*` 等，要求 deterministic 一致（含固定排序、固定聚合順序與固定空值/型別處理）。
- **大窗／長歷史**：分批比對 + 極端 tail 人工抽檢。

### 6.3 驗收輸出（必交）

**三者關係（避免重複 metadata 漂移）**：

- **Feature dependency registry（輸入／單一來源）**：由 deploy `feature_spec.yaml` 依 **§6.1.1** 解析而來；每個 **`(track_section, feature_id)`** 一列，記載所需 L1 欄位、是否允許回掃 bet、計算來源（SQL／程式模組）等。
- **Coverage matrix（registry 的執行狀態）**：同一複合鍵 **`(track_section, feature_id)`**；欄位為「是否已實作、是否已驗證通過、最後驗證 snapshot／窗」等；**不複寫** registry 中已定義之計算邏輯，僅追蹤完成度。
- **Mismatch ledger（coverage matrix 的差異子集）**：僅列 **不一致或未收斂** 之 **`(track_section, feature_id)`**；每列含 root cause、修復 PR／commit、狀態；收斂後可歸檔或從活躍 ledger 移除。

**必交檔案**：

- **Coverage matrix**：列出 deploy spec 每一個 **`(track_section, feature_id)`** 在 asset-layer 的對應欄位、計算來源、是否完成（可由 registry + 狀態欄匯出）。
- **Mismatch ledger**：若任一複合鍵不一致，需逐項列出 root cause 與修復狀態；在達成 100% 覆蓋與一致前不得結案。

---

## 7) 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| 單日分區仍過大導致 OOM | 物化失敗 | 採「估算→參數化執行→OOM 自動降載重試」：先估可用 RAM 與資料量，推導 batch/window；失敗時自動縮窗、提高 bucket 數、降低並行後重試（保留重試紀錄）。 |
| `GAMING_DAY_START_HOUR` 與來源 `gaming_day` 口徑漂移 | run 邊界錯切、特徵不穩定 | 將 cutoff 視為 `definition_version` 參數；變更需升版並重算；例行抽樣比對 `gaming_day` 與 `payout_complete_dtm@HK` 邊界一致性。 |
| 續跑狀態不一致（state corruption） | 已完成分區被重複覆寫、或失敗分區被誤跳過 | 採原子寫入（tmp→rename）、state/manifest 雙重校驗、`--force` 僅允許顯式重算。 |
| `player_id` 碎裂（FND-11） | trip/run 語意與業務直覺不一致 | SSOT 已接受；實作上在監控報告中追蹤「單人多段 trip」比例。 |
| deploy spec 與 asset-layer spec 語義漂移 | 特徵一致性與可維護性下降 | 以 coverage matrix + mismatch ledger 持續稽核，任何新增/修改 feature 必須雙邊對映更新。 |
| registry 與實際表漂移 | 錯誤 event_time | PR 必須更新 registry；CI 驗證欄位存在。 |
| 過早合併進 trainer | 訓練迴歸風險 | Phase 4 前維持並行；合併需另案與回歸 gate。 |

### 7.1 OOM 估算與重試機制（實作契約）

- **Step 1: 前置估算**：啟動時讀取當前可用 RAM（可觀測值）與輸入分區大小、基數估計，計算初始 `window_size` / `player_bucket_count` / `max_parallelism`。
- **Step 2: 執行監控**：執行期間持續監控記憶體水位與 spill 指標；接近風險閾值時優先主動降載（縮小批次/降並行）。
- **Step 3: 失敗重試**：若發生 OOM，按預設階梯重試（例如：`window_size` 砍半 → `bucket_count` 加倍 → `parallelism` 降為 1）；每次重試均寫入 run log。
- **Step 4: 終止條件**：達最大重試次數仍失敗則 fail-fast，輸出可重現錯誤上下文（輸入快照、參數、峰值記憶體）。
- **約束**：重試僅可調整「執行參數」，不得改變業務語義（run/trip 邊界規則、event_time 排序、feature 定義）。
- **Determinism 不變式**：`window_size` / `player_bucket_count` / `max_parallelism` 等執行參數**僅影響資源與執行時間**，**不得**改變 L1/L2 **語義輸出**（列數、主鍵、`run_id`/`trip_id`、特徵值）；聚合與排序須採**固定 reduce 順序**等作法，使不同 batch 切分下結果位元級或契約級一致（見 §8.1 Gate 1）。

---

## 8) 驗收與 Rollout

### 8.1 技術驗收（Gate）

1. Determinism：同 snapshot、**不同** §7.1 執行參數組合下重跑 L1，**hash 與列數**一致；並對約定之關鍵欄位集合做 **row-level canonical hash**（或等價 checksum）抽檢／全量可選，以補「列數相同但內容錯」之漏洞。  
2. Lineage：任一批次可從 manifest 追溯到 L0 分區與 preprocessing 版本。  
3. Membership：`trip_run_map` / `run_bet_map` 可完整重建 run/trip 邊界。  
4. Ingestion：`published` 批次皆含 **ingestion_delay_summary**。  
5. Feature：同 **§6.1**／**§6.1.1**（deploy spec 可枚舉特徵之 **100% 覆蓋**與 **deterministic 一致**）；未達成不得結案。
6. Resume/Idempotency：同日期區間在「一次跑完」與「中斷後續跑」兩種路徑下，輸出列數與 row-level hash 一致；已成功分區可被安全跳過。

### 8.2 Rollout

- **Shadow**：新產線與舊 pipeline 並行寫出，不影響訓練主路徑。
- **Pilot**：單一訓練窗使用 L2 產物做 offline 實驗。
- **Adopt**：經模型 owner 簽核後，才可切換訓練讀取或取代部分 Step。

---

## 9) 開放決策（本計畫不代為決定）

- **線上 K/T/D** 具體數值與 SLO。  
- **L0 不可變儲存**實作（僅追加 vs object key 不可變）。  
- **與 trainer Step 6/7** 最終整合策略（取代、旁路或漸進遷移）。

---

## 10) `late_arrival_correction_log` 最小欄位契約

> 目的：確保延遲到達或修正事件造成的 run/trip 變更可審計、可回放、可量化影響。

### 10.1 儲存與索引

- **主鍵（PK）**：`correction_id`（UUID 或等價，全域唯一）。
- **建議次要索引**：`(player_id, event_time_min)`（時間窗查詢、離線重算掃描）；`(published_snapshot_id_after)`（依已發布版本回溯）；可視查詢模式再加 `(source_snapshot_id)`。

### 10.2 消費契約

- **主要讀者**：**離線重算** job（依 `published_snapshot_id` / 時間窗 / `player_id` 載入 correction 事件，決定需重算的 run/trip／特徵範圍）。
- **非目標讀者**：本契約**不**要求線上 scorer 即時讀取該 log；線上路徑仍以 published snapshot 與有界增量契約為準（見 SSOT §5.4）。
- **保留與 GC**：保留天數、是否可壓縮歸檔、與 L0 不可變策略之對齊，列為 **working plan／運維** 約定（本計畫不定具體天數）。
- **Working plan 待辦**：上述保留天數、壓縮歸檔週期、與 **L0／published snapshot** 生命週期之對齊，須在 **Working plan** 中列為**明確任務與 owner**（本 implementation plan 僅標示缺口）。

### 10.3 最小欄位（語義）

建議最小欄位如下（實作名可調整，語義不可缺）：

| 欄位 | 說明 |
|------|------|
| `correction_id` | 本次修正事件唯一識別碼（UUID 或等價）。 |
| `source_snapshot_id` | 觸發修正的來源快照 ID。 |
| `published_snapshot_id_before` | 修正前對外發布 snapshot。 |
| `published_snapshot_id_after` | 修正後對外發布 snapshot（若尚未發布可為 null + 狀態）。 |
| `entity_type` | 受影響實體：`run` / `trip` / `feature_row`。 |
| `entity_id_before` | 修正前實體 ID（若為新建可為 null）。 |
| `entity_id_after` | 修正後實體 ID（若為刪除可為 null）。 |
| `player_id` | 受影響玩家鍵。 |
| `event_time_min` / `event_time_max` | 受影響事件時間範圍（event-time）。 |
| `observed_at` | 該修正被系統觀測到的時間。 |
| `correction_reason_code` | 修正原因代碼（late_arrival / dedup_fix / status_fix / replay 等）。 |
| `change_summary` | 變更摘要（例：run merge/split、trip reopen/close、feature recompute）。 |
| `upstream_keys` | 上游關聯鍵（例如 `bet_id` 清單或其摘要 hash）。 |
| `definition_version` / `transform_version` | 觸發修正時所用定義與轉換版本。 |
| `operator` | 觸發來源（system/job/manual）。 |
| `created_at` | correction log 寫入時間。 |

最低驗收：

- correction log 可追到對應 manifest 與 published snapshot。
- 可按 `player_id` 與時間窗重建「修正前/後」差異。
- correction log 欄位缺失率為 0（nullable-by-design 欄位除外）。

---

## 11) 文件維護

- SSOT 變更時：本計畫須檢視 **Phase 範圍與驗收** 是否仍成立；必要時升版本計畫「階段」敘述，不修改 SSOT 業務定義。  
- 本計畫版本以文首 **blockquote 版本列**為準（目前 v0.4）；重大架構變更升 minor。

---

*結尾：下一層「工作分解／sprint」屬 Working plan，應由本計畫衍生，不在此文件展開。*
