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
- 訓練資料（training set）產製的版本化與可追溯性。

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
- 產出的 training set 必須攜帶 manifest，記錄來源、版本、列數、特徵服務與關鍵參數。
- 管線必須在一般筆電資源下可執行，不允許預設流程依賴超大記憶體一次載入全量資料。

## 5) 關鍵業務規則與領域定義

- Snapshot：一次完整可用的離線資料快照，作為單次管線輸入邊界。
- Partition：以 `YYYYMM` 為主的月份分區檔，為增量判斷最小粒度。
- Incremental recompute：僅重算新增/變更分區與固定回補視窗內分區。
- Backfill window：為處理晚到資料而保留的重算範圍（例如最近 1 個月，實際值由實作設定）。
- Feature contract：由 Feast `FeatureView` / `FeatureService` 定義的特徵欄位、時間欄位與語義約束。
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
- 對應 manifest（來源指紋、參數、列數、版本、產生時間、特徵服務名）。

## 8) 非功能性需求（NFR）

- 效能：日常迭代不應預設全量重跑；主要工作負載可在本地工作站完成。
- 資源：必須提供記憶體限制、spill 目錄與執行緒控制；避免 OOM 成為預設行為。
- 可重現：同一輸入與設定應得到可重現輸出（允許 metadata 層面的非語義差異）。
- 可觀測：每個主要 stage 有明確輸入、輸出、fingerprint 與執行紀錄。
- 可維護：文件邊界清楚（SSOT vs RUNBOOK vs 實作程式）。

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

## 11) 決策紀錄（Decision Log）

- 採用「分區驅動 + 增量重算」而非每次全量 materialization。
- 採用「DuckDB + dbt-duckdb + DVC + Feast」的分層組合，而非單一工具承擔全部責任。
- Feast 定位為特徵契約與取用層，不承擔中間資料工程快取管理。
- 設定控制面以 YAML/Python 為主，不以環境變數作為主要行為切換機制。

## 12) 開放問題（Open Questions）

- 回補視窗（backfill window）的預設值與調整規則應如何定義（依資料晚到分布）。
- 分區層級 cache fingerprint 是否需要納入 row_count 與 schema hash（除了 size/mtime/path）。
- training set 的批次化 historical retrieval 之標準 chunk 大小（效能與穩定性折衷）。
- 各 stage 的最小監控指標集合（耗時、scan bytes、cache hit ratio）之最終清單。

## 13) 與其他文件的邊界

- 本文件（SSOT）：定義「做什麼、邊界與準則」。
- `RUNBOOK.md`：定義「怎麼操作、怎麼除錯」。
- `README.md`：定義「快速導覽與入口」。
- 實作計畫與執行拆解文件（若需要）應獨立於本文件，且必須追溯本 SSOT。
