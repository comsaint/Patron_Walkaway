# 分層資料資產與 run/trip 特徵工程 — SSOT

> **版本**：v1.6  
> **目的**：定義「可重用、可增量、可追溯」之資料資產組織與特徵工程邊界（單一事實來源，SSOT）。  
> **適用範圍**：`bet → run → trip` 階層化物化、分區策略、主鍵與版本治理、lineage；**不含** train/val/test 切分、閾值與線上評估口徑。  
> **v1.1 變更摘要**：廢除「固定 N 日回補窗口」為核心語義；改為**訓練端版本化完整快照 + 專用清洗規則**，以及**服務端每日離線刷新 + 有界線上狀態修正**（見 §5.3–§5.4）。  
> **v1.2 變更摘要**：新增 **§4.4 事件時間與可觀測時間（`event_time` / `observed_at`）** 契約；離線清洗與 manifest 留痕；**ingestion 延遲摘要 metadata** 供下游監控（本文件不定義 drift 告警規則與閾值）。  
> **v1.3 變更摘要**：固定 **time semantics registry** 路徑為 `schema/time_semantics_registry.yaml`；補充 preprocessing 前置、snapshot-scoped deterministic ID、trip close 語義、membership lineage、以及 current feature spec coverage 契約。  
> **v1.4 變更摘要**：run 定義加入 **gaming day 硬切規則**：除 30 分鐘 gap 外，當事件序跨越 `GAMING_DAY_START_HOUR`（目前專案設定 03:00，Asia/Hong_Kong）亦強制開新 run；同步修訂 §3.1、§4.2、§5.2 與決策紀錄。  
> **v1.5 變更摘要**：§4.4 新增 **邏輯可觀測時間之「殘差 P95 cap」**（`ingest_delay_cap_sec`）：在已文件化之整批入倉／回填時窗排除後，對 `(observed_at_raw - event_time)` 取 **P95** 作為該表 cap 常數；凡延遲超過 cap 之列，**邏輯** `observed_at` 取 `event_time + cap`（**不得**改寫 L0 raw）；`t_bet` 定值 **122 秒**（見 `schema/preprocess_bet_ingestion_fix_registry.yaml` 與決策 **LDA-014**）。  
> **v1.6 變更摘要**：Trip v1 契約補強：`trip_fact` 分區鍵語意固定為 **`trip_start_gaming_day`**；`trip_fact` 必須同時輸出**已關閉**與**進行中** trip（`trip_end_*` 可為 null）；`trip_id` hash 納入 `source_snapshot_id` 與 `first_run_id` 且不受 `trip_end_*` 補值影響；trip close 在不引入外部日曆表前提下，允許僅由 `run_fact` 之有 run 日與缺口推導（缺資料日視為完整一日）；`trip_fact` manifest 需列舉本批次觸及之 `run_end_gaming_day` 分區。  
> **玩家鍵（v1）**：**僅使用 `player_id`**（Smart Table 桌台辨識 ID）作為本資產層之玩家主鍵；**本文件不採用 `canonical_id`** 作為設計依據。

---

## 0) 文件目的與使用說明

本文件為 **長期資料架構（分層物化 + run/trip 聚合）** 的治理規格，供工程與資料科學在實作「增量分區、特徵資產、registry/manifest」時唯一依循之**業務與架構真相**。

- **應由本文件回答**：L0/L1/L2 各層責任、run/trip 定義 v1、分區鍵、**遲到/修正資料在訓練與服務兩條路徑之語義**、**來源表之 `event_time` 與可觀測時間（`observed_at`）契約**（§4.4）、主鍵與版本升級規則、成功度量（平台 KPI）、與既有專案文件之優先序。
- **不應由本文件回答**：切分比例、validation 指標、Optuna、deploy scorer 行為（見 `trainer_plan_ssot.md` 等）。

> **對齊來源（不可互相矛盾之「事實層」）：**
>
> - **原始表語義與 DQ**：`doc/FINDINGS.md`（FND-*）、`schema/GDP_GMWDS_Raw_Schema_Dictionary.md`
> - **來源表時間語義 registry**：`schema/time_semantics_registry.yaml`
> - **`player_id` ↔ `casino_player_id` 映射研究（非本層主鍵）**：`doc/FINDINGS.md` **[FND-11]**、`doc/TRAINER_TEAM_PRESENTATION.md` 附錄 A.1
> - **既有訓練/歸戶建模契約（rated / canonical 路徑）**：`ssot/trainer_plan_ssot.md`、`.cursor/plans/DECISION_LOG.md`（DEC-*）

### 0.1 與 `trainer_plan_ssot.md` 的關係（優先序）

| 主題 | 以何者為準 |
|------|------------|
| **本文件所定義之分層資料資產**（run_fact、trip_fact、feature_run、registry 等） | **本文件** |
| **Walkaway 訓練/推論之 rated、canonical、標籤與 PIT 契約** | **`trainer_plan_ssot.md`**（及 DEC） |
| **兩者未來是否合併**（例如訓練改讀本層資產） | **須另立決策**；合併前不得假設本層已自動取代 trainer 契約 |

**衝突處理原則**：若實作同時觸及「walkaway 訓練管線」與「本資產層」，以 **trainer_plan_ssot** 之建模與防漏語義為準，本文件之 run/trip 資產須能**證明**與該語義一致或可對照後再掛載。

---

## 1) 目標與商業目的

1. **降低重複重算成本**：將最昂貴、最穩定之計算（尤其跨大表之序列彙總）物化為可重用分區資產，避免每次訓練或實驗全量重跑。
2. **支援更豐富之特徵工程**：以 **`trip` 聚合 `run`、`run` 壓縮 `bet`** 之階層，使 trip 級特徵優先由 run 級統計聚合取得，無須反覆掃全量 bet。
3. **可審計與可重現**：任一產出須可追溯至「來源分區、定義版本、特徵版本、轉換版本、資料快照或 hash」。
4. **資源可控**：物化與組裝流程須能在一般筆電級 RAM 與合理時間內完成（具體上限由 implementation plan 約定，本 SSOT 僅要求**不得假設**單機無限記憶體）。

---

## 2) 範圍與非範圍

### 2.1 In scope

- 三層資料資產：`L0 Raw`、`L1 Reusable facts`、`L2 Training-ready assembly`（見 §4）。
- **`bet → run → trip` 領域定義 v1**（見 §3）。
- **分區、增量、遲到資料之訓練/服務語義**（見 §5）。
- **來源與衍生表之可觀測性：事件時間 vs 入湖/可觀測時間**（§4.4）。
- **主鍵規則、版本政策、lineage/manifest 最低欄位**（見 §6–§8）。
- **平台 KPI**（見 §9）。

### 2.2 Out of scope（本文件刻意不包含）

- Train / validation / test **切分**、比例、或任何與「模型選點」相關之策略。
- **`canonical_id` 歸戶**、D2 mapping 產出規則、rated-only 訓練篩選（屬 `trainer_plan_ssot.md`）。
- **線上程式實作細節**（scorer 行程、連線、Validator、MLflow 參數等；除非另案 SSOT 延伸）。**注意**：本文件 §5.4 仍規範「消費本資產時之遲到/snapshot 語義」，與上述程式細節分離。
- **Production ingestion／`observed_at` 漂移之偵測、告警閾值、儀表板與 on-call 流程**（屬監控／營運 SSOT 或另案）；**但**本文件 §4.4、§8、§9 要求 L1/L2 產出須附帶可供該類監控消費之 **ingestion 延遲摘要 metadata**（不得為空規格）。

---

## 3) 名詞與領域定義（v1 定版）

以下定義適用於**本資產層**；若與 `trainer_plan_ssot.md` 用語並列，以 §0.1 優先序為準。

| 術語 | 定義 |
|------|------|
| **`player_id`** | Smart Table 桌台辨識系統指派之玩家識別碼。**本 SSOT 下所有 run/trip 邊界與聚合均以 `player_id` 為唯一玩家鍵。** |
| **`bet`（事件）** | 單筆下注觀測；至少需具備 `player_id`、事件時間（與實作對齊之欄位，如 `payout_complete_dtm`）、`gaming_day`、`bet_id` 等契約欄位（完整欄位表由 implementation plan / schema 補齊）。 |
| **`gaming_day`** | 賭場帳務日；與日曆午夜不對齊。**trip 之分區與「連續 N 個 gaming_day 無下注」之語義以此為準。** |
| **Run（連續下注段）v2** | 同一 `player_id` 之下注序列中，僅當 **(a) 相鄰兩筆 bet 事件時間間隔 ≤ 30 分鐘** 且 **(b) 兩筆 bet 屬同一 `gaming_day`** 時，才屬同一 run。若跨越 `GAMING_DAY_START_HOUR` 造成 `gaming_day` 變更，須**硬切（hard cutoff）**開新 run。 |
| **Trip（行程段）v1** | 同一 `player_id`，若上一筆 bet/run 結束後出現 **3 個完整 `gaming_day` 皆無任何 bet**，則該 trip 才可關閉，下一次 bet 視為**新 trip** 之起點。例：最後 bet/run 結束於 Jan 1，必須 Jan 2、Jan 3、Jan 4 三個完整 gaming day 皆無 bet，該 trip 才在 Jan 4 後可定版關閉。`trip_fact` 必須同時涵蓋已關閉與進行中 trip。 |
| **階層關係** | **`trip` 為 `run` 之聚合；`run` 為 `bet` 之壓縮層。** Trip 級特徵應優先由 run 級可聚合統計得出；僅在契約明列時才允許回掃 bet。 |

### 3.1 設計意圖（非規範細節）

- Run 邊界除 30 分鐘 gap 外，另受 `gaming_day` 邊界硬切；`GAMING_DAY_START_HOUR` 變更視同 run 定義變更，必須升 `definition_version`（見 §5.2、§7）。
- Trip close 是**離線定版語義**；線上服務可維護 provisional trip extension，但不得把未觀測完整 3 個 gaming day 之 trip 當成已最終關閉（見 §5.5）。

---

## 4) 資料資產分層（L0 / L1 / L2）

### 4.1 L0 — Raw（不可變輸入快照）

- 來源事件之**分區原始快照**（例如按日匯出之 `t_bet` 子集或專案約定之 raw parquet）。
- **原則**：L0 寫入後視為該批次之 immutable 輸入；修正走**新版本快照或補寫分區**，不覆寫語義上已發布之唯讀批次（實作細節由 implementation plan 定）。
- **Preprocessing 前置契約**：進入 L1 物化前，必須已套用 dedicated preprocessing step，至少處理 `player_id` 有效性、placeholder/dummy ID 排除、duplicated `bet_id` / 多版本列去重、canceled/deleted/manual 等 DQ 規則。具體規則與 rule id 由 implementation plan 與 `schema/time_semantics_registry.yaml` 的 `dedup_rule_id` / `preprocessing_contract` 對齊。

### 4.2 L1 — Reusable facts（可重用核心）

至少包含下列**邏輯實體**（物理表名由 implementation plan 定）：

| 實體 | 用途 |
|------|------|
| **`run_fact`** | 每個 run 一列；保存 run 級可加總或可合併之統計與時間邊界（供 L2 與 trip 聚合），並應包含 hard cutoff 相關欄位（例如 `is_hard_cutoff` 或等價邊界原因欄位）以供訓練與審計使用。 |
| **`trip_fact`** | 每個 trip 一列；由 `run_fact`（及必要之邊界 metadata）聚合而來。 |
| **`run_day_bridge`（或等價名稱）** | Run 與「bet 所屬 `gaming_day`」之對照，支援日粒度影響分析、審計與重算範圍掃描（即使 run 定義採 `gaming_day` 硬切，仍可作為影響分析輔助）。 |

**原則**：**訓練策略參數**（例如負例抽樣比例）**不得**進入 L1 之失效鍵；該類參數僅能影響 L2。

**Membership lineage（MUST）**：

- 每個 **trip** 必須能追溯其包含之所有 **run**（例如 `trip_run_map` 或等價 membership artifact）。
- 每個 **run** 必須能追溯其包含之所有 **bet**（例如 `run_bet_map` 或等價 membership artifact）。
- `run_day_bridge` 可用於日粒度影響分析，但不得取代 run/trip membership lineage；若 implementation 為降低常態儲存成本選擇壓縮或延遲生成 membership，仍須保證審計與重算時可 deterministically 重建。

**Feature coverage bottom line（MUST）**：

- L1/L2 的最小統計量與 membership 設計由 implementation plan 決定，但必須足以重建目前部署包中的 **`package/deploy/models/feature_spec.yaml`** 所需全部特徵（在本 SSOT 的 `player_id` 粒度下提供等價序列語義；與 trainer canonical 粒度差異須在 implementation plan 明示）。
- 新增 trip-level features 時，應優先由 `run_fact` / `trip_run_map` 聚合；若必須回掃 bet，須在 feature dependency registry 中明示原因與成本。

### 4.3 L2 — Training-ready assembly（輕組裝）

- 依訓練或分析窗口，將 L1 分區拼裝為可用矩陣/檔案（如 parquet、LibSVM 前之身分索引）。
- **抽樣、權重、匯出格式**僅允許出現在 L2（本 SSOT 不定義是否抽樣，但層級歸屬固定為 L2）。

### 4.4 事件時間與可觀測時間（Observability 契約）

本層所有「自來源表讀入之列」與「下游 L1/L2 產物」必須能區分兩類時間語義，以支援 PIT、遲到資料處理（§5）與審計。

| 語義 | 定義 |
|------|------|
| **`event_time`（業務/事件時間）** | 該列所代表之事件**已發生或已結束**之時間，**依表而異**，由 **`schema/time_semantics_registry.yaml`** 逐表鎖定。例：`t_bet` 使用 **`payout_complete_dtm`** 作為 bet 之 `event_time`（與 `trainer_plan_ssot.md` §4.2 對齊；其他表不得硬套同一欄位）。 |
| **`observed_at`（可觀測時間）** | 該列在資料管線中**已可被下游讀取**之時間之上界（ingestion / visibility proxy）。**預設**使用來源表之 **`__etl_insert_Dtm`**；若來源或管線另有更強之「首次可見」時間戳（例如明確之 `first_seen_at`），**應**優先採用並在 registry 註冊。 |

**必須（MUST）**

- 每一張納入本資產層之**來源表**，必須先登錄於 **`schema/time_semantics_registry.yaml`**，至少包含：`table_id`、`business_key`、`event_time` 欄位名、`observed_at` 欄位名（預設 `__etl_insert_Dtm`）、`dedup_rule_id`、是否預期發生 **update/correction**（同一業務鍵多版本列）。
- 衍生計算（run/trip 邊界、排序、增量影響範圍）所依賴之「時間先後」若涉及防漏，**不得**僅以 `observed_at` 取代 `event_time` 作為業務事件序；兩者角色分離。

**已知限制（SHALL 文件化）**

- `__etl_insert_Dtm` **不保證**等同「語意上第一次入湖」：ETL 重跑、補寫、回填可能使該欄位晚於實際首次可服務時間，或與「版本更新」混淆。registry 中須註明是否可能存在 **correction row**（新列取代語義）與如何與 FND-01 類去重規則對齊。

**晚到（late arrival）vs 修正（correction）**

- **Late arrival**：`event_time` 早於、但 `observed_at` 顯著晚於常態延遲（例如長尾回填）；多為**新增列**或延遲到達之列。
- **Correction / update**：同一業務實體之多版本列或欄位事後變更；可能改變 `event_time`、`player_id` 等，進而影響 run/trip 邊界。  
兩者對 `run_day_bridge` 與 §5 之影響分析**須分開**可辨識（implementation plan 定具體欄位或旗標）。

**離線（訓練／物化）**

- 對已知系統性 **`observed_at` 遠晚於 `event_time`** 之案例：須**識別、文件化**，並在訓練用快照中依專案規則**修正或標記**（例如排除、歸檔、或寫入清洗後列）；**不得**無痕改寫已封存審計批次（與 §5.3 一致）。

**離線清洗留痕（MUST）**

- 凡對來源列套用「ingestion 異常修正／排除」規則，該次 snapshot 之 manifest（§8）**必須**包含：`ingestion_fix_rule_id`（或等價）、**規則版本**、受影響列數或時間範圍摘要、以及修正前後 **`ingest_delay`** 分佈摘要（見下段）。

**衍生 ingest delay 與下游監控**

- 定義 **`ingest_delay_sec = observed_at - event_time`**（或等價時間型相減；時區須全專案一致，見 §11）。
- 每一批次 L1/L2 產物之 manifest **建議（SHOULD）** 附帶至少下列**可聚合摘要**（供監控與 drift 偵測消費；**本文件不定義**閾值與告警）：  
  `ingest_delay_p50_sec`、`ingest_delay_p95_sec`、`ingest_delay_p99_sec`、`ingest_delay_max_sec`、`late_row_count`（依專案約定之「晚於預期延遲帶」定義，由 implementation plan 量化）、`late_row_ratio`、`affected_run_count` / `affected_trip_count`（若該批次曾重算 run/trip）。

**邏輯可觀測時間之殘差 P95 cap（MUST；跨表通則）**

- **目的**：抑制已知 **整批入倉／回填** 造成之 `observed_at_raw`（預設 `__etl_insert_Dtm`）相對 `event_time` 之**不可操作長尾**，使 ingest-delay 分析、稽核摘要與（若實作需要）dedup 輔助序與 **FND-13** 一致，**且不改變**業務事件時間與 run/trip 事件序所依之 `event_time`。
- **步驟（每張納入資產層之來源表）**  
  1. **辨識並文件化**已知整批入倉或系統性回填時窗（例：依 `observed_at_raw` 曆日聚集之高峰；證據須可重跑 SQL／附於表別 fix registry）。  
  2. 在**排除上述時窗內之列**（或專案核准之等價母體）上，對  
     `ingest_delay_residual_sec = observed_at_raw - event_time`  
     計算 **`ingest_delay_residual_p95_sec`**（與既有 manifest 摘要使用相同時區與型別語義）。  
  3. 將該值登錄為該表之 **`ingest_delay_cap_sec`**（**無條件取整**：與 measured P95 一致之整數秒；`t_bet` 目前為 **122**，見 `schema/preprocess_bet_ingestion_fix_registry.yaml`）。  
  4. 對**所有**具非空 `event_time` 與 `observed_at_raw` 之列，定義**邏輯**可觀測時間：  
     **`observed_at_logical = min(observed_at_raw, event_time + ingest_delay_cap_sec)`**（時間型語意上等價之 `LEAST` 亦可）。  
     即：若 `ingest_delay_raw_sec > ingest_delay_cap_sec`，則 **`observed_at_logical = event_time + ingest_delay_cap_sec`**；否則 **`observed_at_logical = observed_at_raw`**。  
- **不變式（MUST）**  
  - **L0／raw 快照不得覆寫**來源 `__etl_insert_Dtm`（或 registry 所指之 `observed_at_col`）；`observed_at_logical` 僅允許出現在 **preprocess／L1 衍生欄**（例如 `__etl_insert_Dtm_synthetic`），並須在 manifest 或表別 registry **註冊欄位名與 `ingest_delay_cap_sec`**。  
  - **Run／trip 事件序**仍以 `event_time`（registry 之 `event_time_col`）與專案已定之 `ORDER BY` 為準；**不得**以 `observed_at_logical` 取代 `event_time` 作為業務排序主鍵。  
- **其他表**：尚未完成殘差分佈與排除時窗前，`ingest_delay_cap_sec` **得**標為 **TBD**；一旦採用本通則，須依上列步驟重測並更新 registry／manifest，並升級相關 `transform_version` 或表別 fix 版本。

**Production**

- **ingestion / `observed_at` 漂移之偵測與告警**屬監控域（**out of scope** 於本文件之「規則與閾值」）；惟 L1/L2 **必須**產出上述 metadata，使監控系統無需反向推論即可觀測。

---

## 5) 分區、增量與遲到／修正資料

### 5.1 Trip 分區

- **`trip_fact`（及 trip 衍生特徵）以 `gaming_day` 為標準分區鍵**（與 trip 定義一致）。
- **分區鍵語意（MUST）**：`trip_fact` 分區鍵固定為 **`trip_start_gaming_day`**（trip 第一個 run 所屬 `gaming_day`）。
- **輸出粒度（MUST）**：`trip_fact` 必須同時輸出「已關閉 trip」與「進行中 trip」；進行中 trip 允許 `trip_end_*` 為 null，並須有可機器讀取之閉合狀態欄（例如 `is_trip_closed` 或等價欄位）。
- **計算依賴（MUST）**：v1 不引入外部賭場日曆表；trip close 判定可由 `run_fact` 推導之「有 run 之 `gaming_day`」與其間缺口完成。對於賭場日曆上存在但資料缺席之日，視為一個完整 `gaming_day` 空日。

### 5.2 Run `gaming_day` 硬切與分區（強制設計）

run 定義採 `gaming_day` 硬切後，需滿足：

1. **`run_fact` 主分區**：建議以 **`run_end_gaming_day`**（run 內最後一筆 bet 所屬 `gaming_day`）作為每列分區鍵，與 trip 日分區語義對齊。
2. **硬切判定規則（MUST）**：run 切分須同時套用「30 分鐘 gap」與「`gaming_day` 變更強制開新 run」；其中 `gaming_day` 邊界由 `GAMING_DAY_START_HOUR`（目前專案設定 03:00，Asia/Hong_Kong）決定。
3. **`run_day_bridge`（SHOULD）**：可保留作為日粒度影響分析與審計輔助；不得取代 run/trip membership lineage。

> **禁止**：在同一 `definition_version` 內任意切換 `GAMING_DAY_START_HOUR` 或關閉 hard cutoff。任何此類變更皆須升 `definition_version` 並於 manifest 明載。

### 5.3 訓練資料（離線）：版本化完整快照，不以「固定回補天數」為核心契約

訓練用資料集之準備**不依賴**「僅重算最近 N 日」之固定回補窗口作為語義核心；改採下列原則：

- **版本化輸入快照**：每次訓練或每次 materialization run 必須可指涉 **`source_snapshot_id`**（或等價）、**擷取/凍結時間**、L0 分區與 hash（§8 manifest 擴充欄位由 implementation plan 定）。
- **清洗與 DQ 規則**：得依業務需要掃描完整可得歷史（或該次訓練窗之完整母體），**不以「最多只能回看 N 日」限制清洗語義**；若來源修正改變歷史，產生**新 snapshot / 新分區版本**，不覆寫已封存、已用於已發布模型訓練之輸入批次。
- **遲到或上游修正**：觸發**受控重算**時，範圍由「影響分析」（例如 `run_end_gaming_day` 分區與可選 `run_day_bridge`）決定，而非由固定日曆窗口單獨決定。

### 5.4 服務／線上特徵（資源受限）：已發布快照 + 有界線上增量

Serving 或線上消費本資產時，**不得假設**可對全歷史做無界線即時回補。採用下列雙層語義：

1. **已發布基底（offline refresh）**  
   - 以**定期離線作業**（例如**每日至少一次**；頻率由 implementation plan 與 SLO 定）產出並發布 **`published_snapshot_id`**（或等價）之 L1/L2 產物。  
   - 該快照為該週期內 serving 之**權威基底**；trip 之**定版邊界**原則上以該快照為準（見下段「Run vs Trip」）。

2. **線上增量（bounded）**  
   - 在兩次離線刷新之間，僅允許對**有限範圍**內之資料做線上修正，例如：僅針對**活躍 `player_id`**、僅重播**最近 K 筆 bet 或最近 T 分鐘／D 個 `gaming_day`** 之事件流（K/T/D 由 implementation plan 量化，須可證明上界）。  
   - **Run**：允許在線上以「自最近一次 published snapshot 起之有界狀態」維護 **provisional run**；若 late bet 落入該有界緩衝內，得對該玩家重播近期序列以修正 run 邊界。  
   - **Trip**：**不強制**在線上即時完全重算歷史 trip；線上可使用「snapshot 已定版 trip + 當日／當週期之 provisional 延伸」之語義。**若 late bet 可能改寫已關閉之歷史 trip 邊界**且超出線上有界緩衝：  
     - **不得**在 serving 路徑上做無界歷史重算；  
     - 應將事件寫入 **`late_arrival_correction_log`**（或等價），於**下一次離線刷新**納入正式 L1/L2；  
     - **已發出之歷史告警／已寫入之審計列**不以 retroactive 方式改寫（若業務另需沖帳，須另案規格）。

3. **與 manifest 之對齊**  
   - 線上服務讀取之特徵必須能追溯到 **`published_snapshot_id` + 線上增量版本序號**（若有），避免無法解釋「為何與昨日離線表不一致」。

### 5.5 Run vs Trip（遲到情境之責任切分）

| 層級 | 線上（有界） | 離線（完整） |
|------|--------------|----------------|
| **Run** | 可為主修正對象；於有界緩衝內重播 `player_id` 近期 bets 以修正 provisional run。 | 每日（或約定頻率）刷新之 `run_fact` 為該週期權威。 |
| **Trip** | 以 snapshot 為主；僅允許輕量 provisional 延伸，**不**要求線上完整重算跨多日 trip 邊界。 | 完整 trip 邊界與歷史修正於離線刷新時納入。 |

---

## 6) 主鍵規則（deterministic）

所有主鍵須 **deterministic**：相同輸入、相同 `definition_version`、相同 `source_namespace` 下，重跑必得相同 ID。此 deterministic 保證為 **snapshot-scoped**：late arrival 或 correction 若改變 run/trip 邊界，後續 snapshot 之 `run_id` / `trip_id` 可合理改變，屬預期行為。

| ID | 組成（邏輯） |
|----|----------------|
| **`run_id`** | `hash(player_id, run_start_ts, run_definition_version, source_namespace)` |
| **`trip_id`** | `hash(player_id, trip_start_gaming_day, trip_definition_version, source_namespace, source_snapshot_id, first_run_id)` |

**必須（MUST）**

- `definition_version` 納入 ID 組成或唯一約束，避免 v1/v2 定義混用造成碰撞。
- `trip_id` 一旦生成，不得因後續補寫 `trip_end_*`（例如 trip 關閉後回填結束欄位）而改變；`trip_end_*` 不得作為 `trip_id` hash 輸入。
- 產物附帶 **稽核欄位**：至少含來源分區清單、列數、`built_at`、可選 **content hash**（由 implementation plan 指定演算法）。
- 跨 snapshot 比對不得假設 `run_id` / `trip_id` 永久穩定；若需要追蹤舊新快照之 run/trip 對應，必須透過 membership lineage（run-bet / trip-run）、`source_snapshot_id`、或 implementation plan 定義之 `previous_*_id` mapping。

---

## 7) 版本政策（三軸分離）

| 軸 | 涵義 | 典型升版觸發 |
|----|------|----------------|
| **`definition_version`** | run/trip 邊界語義（如 30 分鐘、3 gaming days） | 改閾值、改「無 bet」之判定口徑 |
| **`feature_version`** | 特徵欄位與計算式 | 新增/修改特徵欄位 |
| **`transform_version`** | 物化流程、引擎、效能實作 | 重寫 pipeline 但不改語義 |

### 7.1 升版語意（摘要）

- **Major**：變更輸出集合或時間語義（與舊版**不可**假設數值相容）。
- **Minor**：新增欄位或向後相容擴充。
- **Patch**：不變更對外語義之修補；若仍導致 hash 變更，下游依 hash 規則失效。

**失效範圍原則**：`definition_version` 變更 → 重算受影響之 L1 與其 L2；`feature_version` 變更 → 僅重算該特徵家族；`transform_version` 變更 → 依「輸出 hash 是否變」決定是否重算。

---

## 8) Lineage / Manifest（最低契約）

每一批次 L1/L2 產出**必須**可寫入或關聯一份 **manifest**（實體可為 JSON、SQLite、DuckDB registry 等，由 implementation plan 定），且至少包含：

| 欄位（邏輯名） | 說明 |
|----------------|------|
| `artifact_kind` | 例如 `run_fact` / `trip_fact` / `feature_run` |
| `partition_keys` | 例如 `gaming_day=2026-04-01` 或 `run_end_gaming_day=...` |
| `definition_version` / `feature_version` / `transform_version` | 與 §7 一致 |
| `source_partitions` / `source_hashes` | 上游 L0 或 L1 分區與指紋 |
| `source_snapshot_id` | 訓練或物化所依據之 L0/L1 快照 ID（若適用） |
| `preprocessing_rule_id` / `preprocessing_rule_version` | L0→L1 前置清洗規則版本（若適用） |
| `row_count` | 該批次列數 |
| `time_range` | 該批次涵蓋之事件時間最小/最大（或專案約定之摘要） |
| `built_at` | 產出時間（UTC 或專案固定時區，需全專案一致） |

**建議（SHOULD）**：若產物屬「已發布 serving 基底」，manifest 或 sidecar 另載 **`published_snapshot_id`**、**離線刷新週期標識**（例如日期）；若產物經線上有界增量疊加，另載 **`online_delta_seq`** 或等價序號（§5.4）。

**建議（SHOULD）**：若該批次曾套用 §4.4 之 ingestion 清洗，另載 **`ingestion_fix_rule_id`**、**`ingestion_fix_rule_version`**、**`ingestion_delay_summary`**（結構化 JSON 或等價，含 §4.4 所列摘要欄位）。

**`trip_fact` lineage（MUST）**：`trip_fact` 每批次 manifest 之 `source_partitions` 必須列舉本批次實際觸及之所有 `run_end_gaming_day` 分區（建議固定排序），`source_hashes` 與該清單對齊。

---

## 9) 成功標準與平台 KPI

本層成功與否以**平台效率與可追溯**為主，不以單一模型 offline 指標為 gate。

| KPI | 定義（摘要） |
|-----|----------------|
| **Reuse rate** | 在既定需求分區集合中，可直接復用、無需重算之 L1/L2 分區占比。 |
| **Recompute ratio** | 單次作業實際重算之分區數（或列數）占需求總量之比例；**目標為持續下降**。 |
| **Time-to-ready (p95)** | 從「提出資料/特徵需求」到「可用快照就緒」之 p95 延遲。 |
| **Ingestion observability coverage** | 每一 published L1/L2 批次是否皆附帶 §4.4 / §8 所要求之 **`ingestion_delay` 摘要**（供監控與事後根因分析）；缺失率應趨近零。 |

**可接受標準（v1 敘述性）**：相較「每次全量重算同等邏輯」之基線，上述效率類指標在穩定運行後應可量化改善；**Ingestion observability coverage** 之缺失率應趨近零。具體數字門檻由 implementation plan 或營運 OKR 另定。

---

## 10) 約束、假設與已知風險

### 10.1 假設

- 歷史資料在**大多數**情況下於某時點後可視為穩定；仍可能發生**無時間上界保證**之晚到或上游修正，故訓練與服務語義須依 §5.3–§5.4 **分軌**處理（訓練重完整與可重現；服務重有界與可負擔）。
- `player_id` 在單次物化輸入批次內可作為穩定 join 鍵；跨批次之一致性由 L0 契約與來源系統保證。

### 10.2 已知風險（採用 `player_id` 之 trade-off）

依 `doc/FINDINGS.md` **[FND-11]**，`player_id` 與 `casino_player_id` 在資料上**並非嚴格 1:1**（雖比例極低）。**本資產層不以 canonical 合併身份**，因此在「換卡 / 系統重發 `player_id`」等情境下，**可能出現同一人之多段 run/trip**，屬 **v1 已接受之產品與技術權衡**。

若未來需與 rated canonical 世界完全對齊，須另開 SSOT 修訂或並行 **`identity_layer`**，不得在未決策下混用兩套鍵於同一產物。

### 10.3 依賴

- 儲存與查詢棧（例如 **Parquet + DuckDB**）須支援分區掃描與增量 append/overwrite 策略（implementation plan 細化）。
- 事件時間與可用時間之防漏原則應與 `trainer_plan_ssot.md` §2.1、§4.2 **一致或可對照**；本層特徵若用於 walkaway 訓練，**不得**放寬 PIT。
- 來源表 **`__etl_insert_Dtm` 受回填污染** 之一般性風險見 `doc/FINDINGS.md` **FND-13**；本層以 §4.4 之 `event_time` / `observed_at` 分離與 manifest 留痕緩解，**不**將系統時間戳誤作業務事件時間。
- Time semantics registry 固定為 **`schema/time_semantics_registry.yaml`**；任何納入本資產層之來源表若未登錄，不得進入 L1/L2 物化。

---

## 11) 開放議題（供下一輪決策）

1. **`source_namespace`** 之枚舉與環境（prod/stage/dev）是否納入 hash 組成。  
2. **`run_start_ts` 時區**：全專案固定為 HK 或 UTC 之單一 SSOT（避免跨系統偏移）。  
3. **L0 批次不可變**之實際儲存策略（僅追加 vs 不可變 object key）。  
4. **與現有 chunk cache / Step 6** 之整合邊界（另案 implementation plan）。  
5. **Serving 有界緩衝之量化**：K/T/D（最近筆數、時間窗、`gaming_day` 數）與活躍玩家定義，須與延遲 SLO 一併驗收。  
6. **`late_arrival_correction_log`** 之最低欄位、保留天數、與離線刷新之合併規則。  
7. **`schema/time_semantics_registry.yaml` 審核流程**：新增來源表、修改 `event_time_col` / `observed_at_col` / `dedup_rule_id` 時，需由何角色核准。  
8. **`late_row_*` 之量化定義**（「預期延遲帶」與表別閾值）是否由監控 SSOT 統一發包或由本層 manifest 固定欄位承載。
9. **Feature dependency registry**：如何從 `package/deploy/models/feature_spec.yaml` 追蹤到 run/trip/bet 所需最低統計量與 membership artifact。

---

## 12) 決策紀錄（本文件內嵌）

| ID | 決策 |
|----|------|
| **LDA-001** | run/trip 定義採 **v2 run + v1 trip**（§3）；後續變更須升 `definition_version`。 |
| **LDA-002** | 玩家主鍵採 **`player_id`**；本層**不使用 `canonical_id`**。 |
| **LDA-003** | Trip 分區以 **`gaming_day`** 為標準。 |
| **LDA-004** | Run 定義採「30 分鐘 gap + `gaming_day` 硬切」；`run_fact` 以 `run_end_gaming_day` 分區，`run_day_bridge` 作為影響分析輔助（§5.2）。 |
| **LDA-005** | **（v1.1 廢止原「固定 7 日回補窗口」語義）** 遲到／修正資料：**訓練**採版本化完整快照與專用清洗，**服務**採定期離線 published snapshot + 有界線上增量；超出有界範圍之歷史邊界變更經 `late_arrival_correction_log` 於下次離線刷新納入（§5.3–§5.5）。 |
| **LDA-006** | 訓練策略參數不得進入 L1 失效鍵（§4.2）。 |
| **LDA-007** | **Run vs Trip 線上責任**：線上以有界修正 **run** 為主；**trip** 以離線定版為主、線上僅允許輕量 provisional 延伸（§5.5）。 |
| **LDA-008** | **雙時間語義**：來源與下游須區分 **`event_time`（表別定義）** 與 **`observed_at`（預設 `__etl_insert_Dtm`）**；區分 **late arrival** 與 **correction**；離線清洗須文件化並 manifest 留痕；L1/L2 須附 **ingestion 延遲摘要** 供監控（§4.4、§8、§9）。 |
| **LDA-009** | Time semantics registry 固定落地於 **`schema/time_semantics_registry.yaml`**；未登錄來源表不得進入本資產層物化（§4.4、§10.3）。 |
| **LDA-010** | `run_id` / `trip_id` 為 **snapshot-scoped deterministic**；late arrival / correction 導致跨 snapshot ID 改變屬預期，跨 snapshot 對照須靠 membership lineage 或另定 mapping（§6）。 |
| **LDA-011** | Trip close 採 **3 個完整 gaming_day 無 bet** 語義；最後 bet/run 在 Jan 1 時，Jan 2–4 必須完整無 bet 才能關閉該 trip（§3）。 |
| **LDA-012** | Trip 必須知道所有 runs，run 必須知道所有 bets；`run_day_bridge` 不得取代 membership lineage（§4.2）。 |
| **LDA-013** | L1/L2 最小統計量由 implementation plan 定，但必須足以重建目前 `package/deploy/models/feature_spec.yaml` 所需全部特徵（§4.2）。 |
| **LDA-014** | 對納入本資產層之各來源表，採 **殘差 P95 cap** 定義 **`observed_at_logical`**（§4.4）：排除已文件化整批入倉後量測 P95，超過 cap 之列令 `observed_at_logical = event_time + cap`；**L0 raw 不改寫**；`t_bet` 之 **`ingest_delay_cap_sec = 122`** 見 `schema/preprocess_bet_ingestion_fix_registry.yaml`（`BET-INGEST-FIX-004`）。 |
| **LDA-015** | Trip v1 落地契約：分區鍵採 `trip_start_gaming_day`、`trip_fact` 同時含已關閉與進行中 trip、`trip_id` 納入 `source_snapshot_id` 與 `first_run_id` 且不受 `trip_end_*` 回填影響、trip close 在不引入外部日曆表下可由 `run_fact` 缺口推導，且 `trip_fact` manifest 必須列舉觸及之 `run_end_gaming_day` 分區（§5.1、§6、§8）。 |

---

*文件結尾：實作細部（表名、目錄佈局、job 編排）屬 Implementation plan，不得在本 SSOT 膨脹為任務清單。*
