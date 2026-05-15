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
- training set 必須提供 Step 4 所需 split keys（`canonical_id`、`gaming_day`）以支援時序切分與可追溯檢核。
- Step 4 必須只承擔 deterministic 前處理（欄位裁切、型別轉換、切分標記）；任何需從資料學習參數的轉換不得在 split 前執行。
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
- Val sub-slices（穩健性評估）：在**固定 val 日期區間**內，依 `gaming_day` 切成 **K 個連續、不重疊**之子區間（v0 預設 **K=4**、等天數；資料不足則 **K 為可切之最大整數** 且 **K≥2**，並於報表揭露），用於計算指標分佈與 **P25**。
- P25 容忍（v0）：於上述子區間上，相對 benchmark baseline（預設 **all history** 訓練政策）之 **ΔAP**、**ΔR@Pmin** 的 **median 與第 25 百分位數**須滿足 `Feature experimentation - WORKING_PLAN.md` §1.4 之數值（其中 **ΔR@Pmin 相關門檻採嚴格上升**，即 **> 0**）；用以避免「中位數改善但尾部切片顯著劣化」之策略過關。
- PIT correctness：每筆實體（entity）僅能看到當時可見的歷史特徵，避免資料洩漏。

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
- 特徵與資料處理設定（YAML/Python 設定檔；不以環境變數作為主要控制面）。
- 標籤與映射相關依賴（例如 canonical mapping、labels artifacts）。

- 輸出（Outputs）
- 清洗後分區資料（cleaned layer）。
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
- Step 3 在最終輸出層補齊 Step 4 split keys（`canonical_id`、`gaming_day`），避免 Step 4 每次重做大表回接，同時維持 Feast retrieval cache 邏輯不變。
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

## 12) 開放問題（Open Questions）

- 回補視窗（backfill window）的預設值與調整規則應如何定義（依資料晚到分布）。
- 分區層級 cache fingerprint 是否需要納入 row_count 與 schema hash（除了 size/mtime/path）。
- training set 的批次化 historical retrieval 之標準 chunk 大小（效能與穩定性折衷）。
- 各 stage 的最小監控指標集合（耗時、scan bytes、cache hit ratio）之最終清單。
- Step 4 的預設時間切分邊界（例如 70/15/15 或固定最近 N 天）與回退規則應如何標準化。
- Gate v0／視窗穩健性 v0 之**審閱週期**與升級為 **v1** 之觸發條件（例如資料量級顯著改變、標籤定義變更）。
- **FQG L2** 之時間穩定性與 MNAR heuristics 是否需要依資料域再校準（§1.5 標註之 first pass 後調整）。

## 13) 與其他文件的邊界

- 本文件（SSOT）：定義「做什麼、邊界與準則」。
- `RUNBOOK.md`：定義「怎麼操作、怎麼除錯」。
- `README.md`：定義「快速導覽與入口」。
- 實作計畫與執行拆解文件（若需要）應獨立於本文件，且必須追溯本 SSOT。
