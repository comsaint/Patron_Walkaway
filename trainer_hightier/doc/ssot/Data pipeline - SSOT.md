# trainer_hightier SSOT（Single Source of Truth）

本文件定義 `trainer_hightier` 離線訓練資料與特徵管線的治理真相來源（SSOT）。  
本文件回答「要建什麼、為什麼、邊界在哪、成功如何定義」，不包含 ticket 級任務拆解或衝刺排程。

## 1) 目標與商業目的

- 建立一套可在本地工作站穩定運行的離線資料與特徵管線，支援 high-tier patron walkaway 預測。
- 在無法穩定使用生產 ClickHouse 寫權限的前提下，仍可快速迭代特徵與模型，降低全量重算成本。
- 確保訓練資料產製具可重現性、可追溯性與時間語義一致性（point-in-time correct）。

## 2) 範圍與非範圍

- 範圍（In Scope）
- 離線來源為分區 Parquet（`t_bet` / `t_session`，以月份分區）。
- **外部 raw 事件來源之 L0 接入與清洗**（例如 `t_casino_txn`）：產出 **source-grain cleaned layer** 與 DQ sidecar；**不含** bet-grain 特徵物化、registry promotion 或 model baseline 變更（見 §5.2）。
- 本地增量資料處理、快取與 artifact 管理。
- 特徵契約治理（Feast feature views/services）與離線 historical retrieval。
- 特徵候選生命週期治理（候選生成、候選分組、**特徵品質閘門 FQG**、候選篩選與升級/淘汰規則）。
- 長窗特徵（例如 180d）之低成本物化策略（monthly snapshot + cache + as-of join）。
- 訓練樣本日期視窗策略（all history vs rolling windows vs recency weighting）之可比較實驗框架。
- 訓練資料（training set）產製的版本化與可追溯性。
- 訓練前資料整理與切分（Step 4）：欄位整理、型別標準化、train/val/test 時序切分。

- 非範圍（Out of Scope）
- 生產 ClickHouse schema/engine/operator 調整與上線流程。
- 即時線上 serving 基礎設施變更（online feature serving infra）。
- 模型演算法策略本身（例如改用何種模型家族）之最終決策。
- **外部來源之 feature crafting**（`txn__*` 等 bet-grain 欄位）、**Gate 1 / ablation 結論**、**registry baseline promotion**——須待來源可信且獨立實驗通過後另案處理；在 **source quarantine** 期間一律不得進 model。

## 3) 利害關係人與使用者

- 資料科學家 / 特徵工程師：需要快速迭代特徵實驗，不可每次全量重算。
- ML 工程師：需要可重現、可回放、可審計的資料與訓練產物。
- 平台/維運：需要可觀測的管線邊界與可控的本地資源使用（RAM/磁碟/CPU）。

## 4) 核心需求

- 離線管線必須以「分區」為基本處理單位，支援增量重算。
- 管線必須具 deterministic cache key，且 cache hit/miss 行為可解釋。
- 特徵定義與取用必須與 Feast 契約一致，並遵守 point-in-time join 原則。
- 候選特徵必須以 feature group 為最小實驗單位管理，不允許預設以全欄位組合做暴力搜尋。
- **特徵品質閘門（Feature Quality Gate，FQG）**：在進入任何 **Gate 1（群組增量）訓練或 ablation** 之前，候選特徵須先通過 **FQG**（兩層：L1 全量、L2 候選入模）；產出 **allowlist / blocklist** 契約並可追溯；**BLOCK** 不得進訓練；**WARN** 僅在明確核准後可進訓練（細節見 `Feature experimentation - WORKING_PLAN.md` §1.5）。
- 候選篩選必須有明確 gate 流程（至少包含 **FQG**、資料品質/契約 **Gate 0**、群組增量 **Gate 1**、群內去冗餘 **Gate 2**）。
- 候選群組升級時，`Recall@Pmin` 相對 baseline 必須**嚴格上升**（`ΔR@Pmin > 0`）；不得以容忍下降方式過關。
- 高相關視窗特徵（如 15m/30m/60m）必須以語意群組方式治理，不得僅以單一相關係數閾值粗暴刪除。
- 長窗特徵必須允許低頻更新（例如月錨點快照），並具 freshness/staleness 可觀測欄位。
- 訓練視窗策略實驗必須固定同一 eval 區間（val/test）比較，不允許因切分改變造成不公平結論。
- 若縮短訓練資料準備範圍，必須滿足 `train_start - max_lookback - safety_buffer` 的資料覆蓋規則。
- 產出的 training set 必須攜帶 manifest，記錄來源、版本、列數、特徵服務與關鍵參數。
- training set 必須提供 Step 4 所需 split keys（`canonical_id`、`gaming_day_event`）以支援時序切分與可追溯檢核。
- Step 4 必須只承擔 deterministic 前處理（欄位裁切、型別轉換、切分標記）；任何需從資料學習參數的轉換不得在 split 前執行。
- Day 1 遷移採全歷史回填，分區鍵同步切換到 `gaming_day_event` 語意；舊分區鍵產物不得作為新語意流程輸入。
- 管線必須在一般筆電資源下可執行，不允許預設流程依賴超大記憶體一次載入全量資料。
- 實驗結果必須納入營運承接容量護欄：`val alerts/hour` 平均不得超過 **120**（約 2 alerts/min）；超限必須觸發告警並在決策中標註。
- 候選特徵篩選（Gate 0/1/2）與訓練視窗策略升級之**定量門檻**採 **v0**（與 `Feature experimentation - WORKING_PLAN.md` §1.4 一致）；調整門檻需更新該文件並於本文件 **決策紀錄** bump 版本說明。

## 5) 關鍵業務規則與領域定義

- Snapshot：一次完整可用的離線資料快照，作為單次管線輸入邊界。
- Partition：以 `YYYYMM` 為主的月份分區檔，為增量判斷最小粒度。
- Incremental recompute：僅重算新增/變更分區與固定回補視窗內分區。
- Backfill window：為處理晚到資料而保留的重算範圍（例如最近 1 個月，實際值由實作設定）。
- Feature contract：由 Feast `FeatureView` / `FeatureService` 定義的特徵欄位、時間欄位與語義約束。
- Feature group：以 `entity key + cadence + compute pattern + dependency` 劃分的候選特徵群組，為最小實驗與快取治理單位。
- Candidate registry：候選特徵與群組的版本化目錄，記錄語意、依賴、成本與篩選狀態。
- **Feature Quality Gate（FQG）**：以 **欄位（feature column）** 為粒度、跨 **train/val/test（或等價 split）** 之離線品質檢查；**L1** 為全候選欄位必跑之輕量檢查；**L2** 為僅對「擬入模之候選欄位」執行之較重檢查（分布漂移、時間穩定性等）。狀態：**PASS**（可進後續 gate）、**WARN**（需顯式核准）、**BLOCK**（不得進訓練，流程預設 **fail-fast**）。
- **FQG allowlist / blocklist**：由 FQG 產出之機讀清單；訓練與實驗 runner 僅允許使用 allowlist 內欄位（WARN 僅在核准 metadata 存在時列入 allowlist）。
- Training window strategy：訓練樣本時間範圍政策（全歷史、固定 rolling window、時間衰減權重）。
- `gaming_day_event`：跨表統一事件日欄位（`t_bet`、`t_session` 等），定義為「事件時間轉換至 `Asia/Hong_Kong` 後的日曆日（00:00 切日）」；為本專案唯一日級切分語意，全面取代舊 `gaming_day`。
- **遷移政策**：採 **Day 1 full migration**，不採雙寫／雙讀；新流程僅接受 `gaming_day_event`。
- **時間欄位標準化**：cleansing 階段將**所有 timestamp 欄位（含 Included 與 Excluded）**統一轉為 `Asia/Hong_Kong` 且以 **HK tz-aware** 型態落盤；重跑同一輸入必須維持 idempotent（不得二次位移）。
- **日期欄位規則**：`DATE` 類欄位不做 timezone 轉換；僅由新規則衍生 `gaming_day_event`。
- **事件時間白名單（v1）**：`t_bet.event_time = payout_complete_dtm`（NULL 直接 drop）；`t_session.event_time = session_end_dtm`（NULL 直接忽略，不做 fallback）。
- **早期健全性檢查（hard-fail）**：在資料管線前段檢查 `__etl_insert_Dtm >= event_time`；任何 `__etl_insert_Dtm < event_time` 的列皆視為契約違反並中止流程。
- Val sub-slices（穩健性評估）：在**固定 val 日期區間**內，依 `gaming_day_event` 切成 **K 個連續、不重疊**之子區間（v0 預設 **K=4**、等天數；資料不足則 **K 為可切之最大整數** 且 **K≥2**，並於報表揭露），用於計算指標分佈與 **P25**。
- P25 容忍（v0）：於上述子區間上，相對 benchmark baseline（預設 **all history** 訓練政策）之 **ΔAP**、**ΔR@Pmin** 的 **median 與第 25 百分位數**須滿足 `Feature experimentation - WORKING_PLAN.md` §1.4 之數值（其中 **ΔR@Pmin 相關門檻採嚴格上升**，即 **> 0**）；用以避免「中位數改善但尾部切片顯著劣化」之策略過關。
- PIT correctness：每筆實體（entity）僅能看到當時可見的歷史特徵，避免資料洩漏。

### 5.1) 特徵四層與 Short-term 離線 PIT cache（Step 3.5）

與 [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) §「特徵四層與 Short-term PIT」對齊：

| 層 | Step 3 / 3.5 產物 | 說明 |
|----|-------------------|------|
| **raw** | `training_set.parquet` 內 cleaned 欄位 | 當筆 passthrough |
| **short** | `_main_trainer_fe_short_term.parquet` → enrich 後進 `training_set_fe_enriched.parquet` | **離線 PIT cache**：對訓練集每個 `bet_id` 用 bounded hot pool 算 PIT（含 `bet__*` 與 short `fe__*`），**語意仍為 PIT**，非 mid 式日聚合表 |
| **mid** | `_main_trainer_mid_term_daily_snapshot.parquet` + enrich ASOF | 日快照 + composite |
| **long** | Feast slow join（Step 3 month-batch） | 月快照 ASOF |

- **勿**將 `fe_short_term` 檔名理解為「非 PIT 預計算特徵」；正確稱呼為 **short-term PIT cache**（manifest 鍵 `fe_short_term_parquet` 保留相容）。
- **勿**用訓練 cache 供應 production 未見 `bet_id`；生產打分見 Scorer SSOT（live PIT 主路徑）。
- `bet__*` 與 short `fe__*` 屬**同一 short 層**；registry `source: feast_trial_1h` 為歷史標籤，訓練供應為 `short_term_pit_builder`。

### 5.2) 外部 raw 事件來源接入（L0 only；`t_casino_txn` v1）

在既有 `t_bet` / `t_session` L0 之外，允許納入 **CDC 事件表** 作為 **cleaned source layer** 輸入。第一案為 `t_casino_txn`（細節見 `doc/FINDINGS.md` **[FND-19]**、`schema/GDP_GMWDS_Raw_Schema_Dictionary.md` §5）。

**分層原則（強制）**

| 層 | 產物 | 本階段 |
|----|------|--------|
| **L0 cleaned source** | `cleaned__gmwds_t_casino_txn/`（source-grain parquet + manifest） | **In scope** |
| **L1 feature materialize** | bet-grain `txn__*` 欄位、experiment materializer | **Out of scope**（另案） |
| **Training / model** | Step 3.5 enrich、registry、Gate 1 | **Out of scope**；quarantine 期間 **not_model_eligible** |

**時間語意（`t_casino_txn` v1，已鎖定）**

| 欄位語意 | 欄位 | 規則 |
|----------|------|------|
| **Event time** | `start_dtm` | 業務事件發生時間；PIT 比較基準（對 bet 為 `payout_complete_dtm` 時，屬 L1 議題，L0 僅物化並保留） |
| **Observed-at** | `__etl_insert_Dtm` | 入湖可觀測時間 |
| **Available time（保守）** | `txn_available_ts` | **v1 定義為 logical observed-at**：`GREATEST(LEAST(__etl_insert_Dtm, start_dtm + 128s), start_dtm)`；不假設 raw `start_dtm` 即時可見，也不允許事件發生前可見 |
| **Raw path** | `data/t_casino_txn/partition_YYYYMM/part_*.parquet` | 固定本地來源根目錄；不使用 `data/new tables` 單檔樣本作正式來源 |
| **Partition / audit** | `gaming_day` / `partition_YYYYMM` | 僅分區與稽核；**不得**作 event time 或 PIT anchor；月份分區允許因 casino day cutover spill 到鄰近日 |
| **型別契約（L0 materialize）** | DuckDB `TIMESTAMPTZ` | L0 explicit CAST 與 registry SQL 一律使用 `TIMESTAMPTZ`（非 `TIMESTAMP`）；**DDL ground truth** = `schema/schema.txt` → `GDP_GMWDS_Raw.t_casino_txn`；L0 cast 契約 = `txn_l0_schema.py`；人類可讀欄位說明 = dictionary §5 |

**L0 清洗（v1，已鎖定）**

- **Logical key**：`casino_txn_id`；delete-aware dedup（任一版本 `__op='d'` 或 `__deleted='True'` → 整筆 logical id 排除）。
- **Dedup 排序**：`__etl_insert_Dtm DESC, updated_dtm DESC`。
- **Type 範圍**：L0 **保留所有 `type`**（不在 L0 過濾為 BUYIN/CASHOUT only）；type/status 篩選留待 **L1 feature materializer**。
- **Hard exclude（列級）**：缺 `casino_txn_id` 或缺 `start_dtm` 或缺 `__etl_insert_Dtm` → 不進 cleaned output（sidecar 記錄計數）。
- **Raw observed-before-event preflight（來源級）**：raw `__etl_insert_Dtm < start_dtm` 必須被 preflight 捕捉並輸出 evidence。若無登錄 correction rule 覆蓋 → **hard-fail**；若符合已登錄 bulk/correction episode → 可 materialize，但必須寫入 logical `txn_available_ts`、保留 raw `__etl_insert_Dtm`、標記 `observed_at_correction_rule_id`，並在 sidecar 記錄修正列數。
- **登錄 correction（v1）**：`TXN-BULK-INGEST-2025-05-27`；`ingest_delay_cap_sec = 128`（txn residual P95，排除 2025-05-27 bulk observed-at day）；logical observed-at:
  `GREATEST(LEAST(TRY_CAST(__etl_insert_Dtm AS TIMESTAMPTZ), TRY_CAST(start_dtm AS TIMESTAMPTZ) + INTERVAL 128 SECOND), TRY_CAST(start_dtm AS TIMESTAMPTZ))`。
- **Partial partition 偵測（audit-only，非 hard-fail）**：月份分區邊界可能僅含部分 shard 或列數明顯低於同批 sibling 月份。L0 materialize 須寫入 sidecar `partition_coverage` / `is_partial_partition`，供下游 **不得** 將該月視為完整月。閾值（與 `preprocess_l0_data_contract_registry.yaml` → `integration_contract.partial_partition_policy` 一致）：
  - `post_dedup_rows < 100_000` → `post_dedup_rows_below_absolute_floor`
  - `post_dedup_rows / sibling_median_post_dedup_rows < 0.20`（sibling 取自已 materialize 之其他 `partition_YYYYMM` sidecar）→ `post_dedup_rows_below_sibling_median_ratio`
  - `shard_count ≤ 3`（`part_*.parquet` 計數）→ `shard_count_at_or_below_partial_threshold`
  - 任一 reason 觸發即 `is_partial_partition: true`；**仍產出 cleaned parquet**（quarantine 語意不變）。
- **Suspicious 列**：保留於 cleaned output 並打 **invalid/suspicious flag**（例如非正 `txn_value`、非預期 type/status 組合）；供 DQ 與事故調查。
- **Join 備註**：`bet_id` / `session_id` 在 raw 多為 NULL；L0 不承諾 bet-grain join；實體鍵以 `player_id` 保留供稽核（canonical 映射屬後續議題）。

**Source quarantine（資料事故期間，強制）**

- 因上游 **data source incident**，`t_casino_txn` cleaned artifact 預設標記 **`not_model_eligible`**。
- Quarantine 期間：允許 **ingest、L0 preprocess、DQ report、investigation**；**禁止** registry 引用、Step 3.5 enrich、feature experiment 之 Gate 1 結論作為 promote 依據。
- 解除 quarantine 須有 **explicit decision record**（非本文件範圍）。

**Quarantine exit checklist（SSOT gate；全部必須成立）**

| 類別 | 退出條件（必須成立） | 必備證據 |
|------|----------------------|----------|
| **Schema contract** | `schema/schema.txt`、dictionary §5、`txn_l0_schema.py` 三者無 drift；欄位順序、ClickHouse 型別、nullable/default、L0 cast 契約一致。 | 自動驗證測試與對應 fingerprint / contract reference。 |
| **Source completeness** | 擬解除 quarantine 的來源快照不得靜默納入 `is_partial_partition: true` 月份；partial 月必須補齊重跑，或在 decision record 中明示排除。 | `source_metadata.json`、`txn_l0_materialization_report.json` 之 `partition_coverage` 與 `partial_partition_reasons`。 |
| **CDC correctness** | delete-aware dedup、delete marker 排除、observed-before-event correction 均已穩定；不得存在**未登錄** correction episode 或未解釋的 CDC 邏輯漂移。 | `txn_l0_preflight_report.json`、`txn_l0_materialization_report.json`、correction rule evidence。 |
| **Time semantics / PIT safety** | `start_dtm` / `txn_available_ts` / `__etl_insert_Dtm` 的角色定義維持一致，且不得存在未受控的 observed-before-event 或 PIT leakage。 | preflight evidence、delay distribution、PIT 驗證結果與對應 rule id。 |
| **Business semantics** | 擬用於 model 的 `type` / `status` / `sub_type` 規則已被明確定義並經 domain review；特別是 `BUYIN`、`CASHOUT`、`Prize Redemption` 等邊界案例需有明示納入/排除決策。 | 規則說明、DQ 切片、domain sign-off 或等價決策紀錄。 |
| **Join / entity readiness** | 首個 model-eligible use case 的 join grain 與實體鍵已鎖定，且不得偷偷依賴已知高缺失鍵（如 `bet_id`、`session_id`）作核心連接。 | use-case 說明、coverage 摘要、key 選擇 rationale。 |
| **Promotion evidence boundary** | quarantine 期間的 `txn_lite` / Gate 1 歷史結果最多只能作背景參考，**不能單獨**作為解除 quarantine 或 registry promote 依據。 | decision record 需明列採納與排除的證據來源。 |
| **Governance release** | 解除 `not_model_eligible` 必須是**顯式、可追溯、可回退**的治理決策，至少要指明適用 snapshot / 月份範圍、允許用途、已知排除項、批准人與回退條件。 | explicit decision record。 |

- **退出範圍預設為 snapshot-scoped**：除非 decision record 明確說明，解除 quarantine 應僅適用於經審核之特定 snapshot / 月份範圍，不應自動外推至未審核的新分區。

**L0 契約產物（每輪 materialize 至少一份）**

- Cleaned parquet（`cleaned__gmwds_t_casino_txn/`）。
- `txn_l0_materialization_report.json`（row counts、dedup、null rate、type 分布、delay 分布、raw observed-before-event evidence、correction rule application、`partition_coverage`、schema/cleaning fingerprint）。
- `txn_l0_preflight_report.json`（preflight evidence、`shard_count`）。
- `source_metadata.json`（`cleaning_policy_id`、`source_contract_ref`、raw partition fingerprint、`is_partial_partition`、`partial_partition_reasons`）。

**實作計畫**：`doc/implementation/active/t_casino_txn Source Integration - IMPLEMENTATION_PLAN.md`。

## 6) 架構真相（Architecture SSOT）

- DuckDB：本地資料處理與查詢執行引擎（含 spill 與記憶體上限控制）。
- dbt-duckdb：轉換邏輯 DAG、增量模型與依賴關係治理層。
- DVC：大型資料 artifact 與管線 stage 快取、版本追蹤與可重現執行層。
- Feast：特徵契約與 historical feature retrieval 層，不負責中間步驟物化快取。

責任邊界：
- 「資料增量物化與中間資產快取」屬於 DuckDB + dbt-duckdb + DVC。
- 「特徵定義、組合與 PIT 取用」屬於 Feast。

## 7) 輸入/輸出契約（Contract-Level）

- 輸入（Inputs）
- 分區 Parquet snapshot（`t_bet__part_YYYYMM.parquet`、`t_session__part_YYYYMM.parquet`）。
- 外部事件來源分區 Parquet（`t_casino_txn` 固定為 `data/t_casino_txn/partition_YYYYMM/part_*.parquet`）；須可追溯至 `source_manifest_v2` 或等價 inventory（Phase D，見 txn Source Integration IP）。
- 特徵與資料處理設定（YAML/Python 設定檔；不以環境變數作為主要控制面）。
- 標籤與映射相關依賴（例如 canonical mapping、labels artifacts）。

- 輸出（Outputs）
- 清洗後分區資料（cleaned layer）：`t_bet`、`t_session`、以及 **外部來源**（例如 `cleaned__gmwds_t_casino_txn/`）。
- 外部來源 L0 sidecar：`txn_l0_materialization_report.json`、`txn_l0_preflight_report.json`、`source_metadata.json`（§5.2；含 `partition_coverage` / partial partition 訊號）。
- 特徵分區資料（feature layer，例如 trial/slow）。
- 訓練資料快照（training set parquet）。
- 候選篩選報表（group-level uplift、去冗餘決策、淘汰理由）。
- **FQG 契約產物（每輪或每 training set fingerprint 至少一份）**：`feature_quality_report.json`（逐欄檢查結果與證據摘要）、`feature_allowlist.json`、`feature_blocklist.json`（含 **reason_code**）。
- 訓練視窗策略比較報表（固定 eval 區間下的效能與成本對照）。
- Step 4 切分產物（`train` / `val` / `test` 或等價 `split_tag` 單檔）。
- 對應 manifest（來源指紋、參數、列數、版本、產生時間、特徵服務名）。
- 對應 split report（切分邊界、各 split 列數/label 比例、canonical 覆蓋與冷啟動占比）。

## 8) 非功能性需求（NFR）

- 效能：日常迭代不應預設全量重跑；主要工作負載可在本地工作站完成。
- 資源：必須提供記憶體限制、spill 目錄與執行緒控制；避免 OOM 成為預設行為。
- 可重現：同一輸入與設定應得到可重現輸出（允許 metadata 層面的非語義差異）。
- 可觀測：每個主要 stage 有明確輸入、輸出、fingerprint 與執行紀錄。
- 可維護：文件邊界清楚（SSOT vs RUNBOOK vs 實作程式）。
- 實驗治理：每輪特徵與視窗實驗必須有固定報表欄位與 go/no-go 決策記錄。
- 告警治理：報表必須揭露 `alerts/hour` 與 `capacity_alarm` 狀態，供營運容量審核。

## 9) 限制、假設、依賴

- 限制（Constraints）
- 開發環境不保證可寫入生產 ClickHouse 物化物件（例如 MV）。
- 本地計算資源有限，需避免以 pandas 全量載入作為預設路徑。
- 資料來源可能為大量分區檔案，I/O 與 metadata 成本不可忽視。

- 假設（Assumptions）
- 執行管線前，輸入分區檔案已完整下載且可讀。
- 分區命名規則穩定且可推導月份。
- Feast registry 與定義檔可在本地環境正確載入。

- 依賴（Dependencies）
- DuckDB / dbt-duckdb / DVC / Feast 及其相容版本。
- 既有 `trainer_hightier` contracts 與資料語義定義。

## 10) 成功標準 / 高層驗收準則

- 能在不依賴生產 DB 寫權限下完成端到端 training set 產製。
- 相鄰兩次 run 若僅少數分區變動，主要 stage 可命中快取且顯著縮短總耗時。
- 產出的訓練資料具完整 manifest，可追溯來源分區與特徵契約版本。
- 離線特徵擷取符合 PIT 語義，避免明顯資料洩漏風險。
- 在筆電資源限制下，預設流程可穩定執行且無系統性 OOM。
- 候選篩選流程可在固定評估口徑下產出可比較結論，並可重現地淘汰無效特徵群組。
- 任何進入 Gate 1／ablation 之特徵集合均已通過 **FQG** 並具 **allowlist** 可追溯；存在 **BLOCK** 時預設中止後續訓練（fail-fast），不得靜默略過。
- 候選群組升級決策符合目標函數：相對 baseline 的 `Recall@Pmin` 嚴格上升，且 `val alerts/hour ≤ 120`（超限須告警且不得默認通過）。
- 訓練視窗策略可量化比較效能與成本，並支持升級為標準策略或回退。

## 11) 決策紀錄（Decision Log）

- 採用「分區驅動 + 增量重算」而非每次全量 materialization。
- 採用「DuckDB + dbt-duckdb + DVC + Feast」的分層組合，而非單一工具承擔全部責任。
- Feast 定位為特徵契約與取用層，不承擔中間資料工程快取管理。
- Step 3 在最終輸出層補齊 Step 4 split keys（`canonical_id`、`gaming_day_event`），避免 Step 4 每次重做大表回接，同時維持 Feast retrieval cache 邏輯不變。
- 時間語意遷移採 Day 1 full migration：全表 timestamp（含 Excluded）統一為 HK tz-aware；`DATE` 欄位不做時區轉換，僅衍生 `gaming_day_event`。
- 事件時間白名單 v1：`t_bet` 用 `payout_complete_dtm`（NULL drop）；`t_session` 用 `session_end_dtm`（NULL ignore，與 `schema/time_semantics_registry.yaml` 對齊）。
- 導入早期 hard-fail sanity gate：`__etl_insert_Dtm < event_time` 即中止管線；`__etl_insert_Dtm >= event_time` 視為合法。
- 遷移批次一次性清空並重建 cache/manifest，避免舊語意 artifact 混入。
- Step 4 為訓練前資料整理與切分專屬階段，目標為「同一批玩家的未來預測」（時間泛化優先）。
- `trainer.py` 提供 `--start-from-features` 作為流程入口控制：允許跳過 Step 1-3，直接以既有 training parquet 啟動 Step 4。
- 候選特徵實驗採 group-first 策略：先比較群組增量，再做群內去冗餘，不做預設全組合。
- 長窗特徵採低頻快照（預設月錨點）+ 快取重用，並保留 staleness 訊號以避免隱性訊息鈍化。
- 訓練視窗策略視為可實驗政策，不預設全歷史最佳；採固定 eval 區間比較法做決策。
- 設定控制面以 YAML/Python 為主，不以環境變數作為主要行為切換機制。
- **FQG v0**：採 **L1（全量）+ L2（候選入模）** 兩層；計算採 **每 split 抽樣上限（預設 200k 列）**、**固定 random seed**、統計以 **float32** 為預設以降低 RAM；時間穩定性預設以 **月** 切片；**WARN 不預設擋訓練** 但須 **顯式核准** 方可列入 allowlist；**BLOCK 觸發 fail-fast**；數值門檻與 reason code 以 `Feature experimentation - WORKING_PLAN.md` §1.5 為準。
- 候選篩選 Gate 0/1/2 採 **v0 數值門檻**（缺失率、常數率、非法值、PIT、ΔAP、ΔR@Pmin、相關係數群聚等）；細節以 `Feature experimentation - WORKING_PLAN.md` §1.4 為準；**Gate 0 之輸入特徵集合不得包含 FQG BLOCK 欄位**。
- 候選群組升級之業務護欄：`ΔR@Pmin > 0`（嚴格上升）且 `val alerts/hour ≤ 120`，超限需觸發容量告警並於決策紀錄揭露。
- 訓練視窗策略升級採 **v0 穩健性規則**：固定 val 內 **K 子區間**上之 **median 與 P25**（對 ΔAP、ΔR@Pmin）與絕對 **round runtime** 上限；細節同上。
- **`t_casino_txn` L0 v1（2026-06）**：外部來源先接 **cleaned source layer only**；`txn_available_ts` 採 logical observed-at（txn residual P95 cap 128s + event-time floor）；L0 保留全 type；**source quarantine / not_model_eligible** 直至上游事故關閉；feature crafting 與 registry 另案（見 txn Source Integration IP）。
- Feature experimentation 中 `txn_lite` / Gate 1 歷史結果 **不得**作為 quarantine 期間之 model 或 registry 決策依據。

## 12) 開放問題（Open Questions）

- 回補視窗（backfill window）的預設值與調整規則應如何定義（依資料晚到分布）。
- 分區層級 cache fingerprint 是否需要納入 row_count 與 schema hash（除了 size/mtime/path）。
- training set 的批次化 historical retrieval 之標準 chunk 大小（效能與穩定性折衷）。
- 各 stage 的最小監控指標集合（耗時、scan bytes、cache hit ratio）之最終清單。
- Step 4 的預設時間切分邊界（例如 70/15/15 或固定最近 N 天）與回退規則應如何標準化。
- Gate v0／視窗穩健性 v0 之**審閱週期**與升級為 **v1** 之觸發條件（例如資料量級顯著改變、標籤定義變更）。
- **FQG L2** 之時間穩定性與 MNAR heuristics 是否需要依資料域再校準（§1.5 標註之 first pass 後調整）。
- `t_casino_txn`：`player_id` vs `canonical_id` 作為未來 L1 join grain 是否升級（L0 先保留 `player_id`）。
- `t_casino_txn`：available time 是否在 v2 引入 type-specific complete 時間，或是否將 `TXN-CORRECTION-2025-10-16` 登錄為額外 correction episode（會使 residual cap 接近 122s）。

## 13) 與其他文件的邊界

- 本文件（SSOT）：定義「做什麼、邊界與準則」。
- `RUNBOOK.md`：定義「怎麼操作、怎麼除錯」。
- `README.md`：定義「快速導覽與入口」。
- 實作計畫與執行拆解文件（若需要）應獨立於本文件，且必須追溯本 SSOT。
- **`t_casino_txn` source integration**：`t_casino_txn Source Integration - IMPLEMENTATION_PLAN.md`（L0 only）；**不得**與 `Feature experimentation - IMPLEMENTATION_PLAN.md` 之 feature crafting 混寫。
- **Feature experimentation**：txn_lite 歷史實驗為背景參考；quarantine 期間不作 feature 決策依據。
