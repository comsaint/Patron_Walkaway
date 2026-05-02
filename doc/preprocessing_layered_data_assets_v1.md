# Preprocessing 規格（分層資料資產 L0→L1）— v1

> **對齊**：`schema/time_semantics_registry.yaml` 之 `dedup_rule_id` / `preprocessing_contract`；SSOT `layered_data_assets_run_trip_ssot.md` §4.1、§4.4；FINDINGS **FND-01 / FND-03 / FND-11 / FND-12 / FND-13**（及關聯 FND）。  
> **Manifest**：清洗套用後，批次 manifest 須可寫入 `preprocessing_rule_id` / `preprocessing_rule_version`（見 SSOT §8）。

---

## 0) 定案摘要（本文件 v1）

| 議題 | 決定 |
|------|------|
| **`player_id = -1`** | **一律排除**，不得進入 L1 物化下注流（與 `trainer/identity.py` 之 placeholder 常數一致）。 |
| **Unrated / non-rated `player_id`** | **一律及早排除**：本專案僅對 rated players 建模與推論（`ssot/trainer_plan_ssot.md` §2.2、§3.3、§6.2）；LDA pipeline 雖以 `player_id` 為 run/trip 主鍵，但進 L1 前須先用 canonical mapping eligibility 判斷並剔除無卡客／unrated players。 |
| **FND-12 dummy `player_id`** | **強制排除**：須與現行 trainer 之 dummy 偵測語義對齊後，於 session / canonical eligibility 路徑產出可引用之集合，並於 bet 進 L1／特徵前剔除（見 §2.3、§4 `SES-FND12-01`、§7）。 |
| **Backfill / observed delay** | 對每張納入管線之表，以 `observed_at_col`（通常 `__etl_insert_Dtm`）減 `event_time_col` 做歷史分析；threshold policy 於表別分析後決定。已知重大回填 episode 須文件化，不得 silent pass。 |
| **`t_shoe`** | **本輪不納入**：不進 L1／不開發 `preprocess_shoe_v1` 產線；registry 列可保留作占位，實作忽略直至另案啟用。 |
| **`t_game` 進 L1/L2** | 目前只採與其他表一致的 `observed_at` / `event_time` 延遲分析與 `game_id` 去重底線；因尚未研究，不額外過濾、不給特徵引用，直到另案完成 game 表 DQ。 |
| **`session_end_dtm` 缺失列** | **直接排除**（不 fallback 至 `lud_dtm` 作為本資產層 event time）。以本 repo 目前 `data/gmwds_t_session.parquet` 全表掃描為參考：`session_end_dtm IS NULL` 約 **97,574 / 83,683,171（≈0.12%）**；新匯出批次應重掃並寫入 manifest／QA 摘要。 |
| **Eligibility sidecar 目錄（方案 A）** | 定案：**`data/layered_assets_sidecar/<source_snapshot_id>/`**（專用子目錄，不與 raw `gmwds_*` 混放）；檔案格式見 **§5.1**。 |

---

## 1) Rule ID 一覽（`preprocess_*_v1`）

| Rule ID | 來源表（registry） | 目的（摘要） |
|---------|-------------------|-------------|
| `preprocess_bet_v1` | `t_bet` | 下注列可進 L1：`player_id` / `bet_id` 有效（**含排除 `player_id = -1`、unrated、dummy**）、去重、排除作廢與非法列；事件序以 `payout_complete_dtm` 為準。 **實作（MVP）**：`scripts/preprocess_bet_v1.py`（DuckDB；可選 `--dummy-player-ids-parquet`／`--eligible-player-ids-parquet`）。 |
| `preprocess_session_v1` | `t_session` | Session 輔助列：`session_id` 去重版本、身分欄位清洗、人工帳務／幽靈 session 路由、canonical eligibility、**rated eligible / FND-12 dummy `player_id` 集合**供下游剔除。 |
| `preprocess_game_v1` | `t_game` | 牌局列去重與 observed delay 分析；尚未完成研究前不額外過濾、不進特徵引用。 |
| `preprocess_shoe_v1` | `t_shoe` | **本輪不使用**：不實作進 L1；字典占位可保留。 |

版本標籤建議：`preprocessing_rule_version` 與本文件標頭 **v1** 對齊；細部版次可用 `v1.0.0` semver 延伸（由實作決定，但須寫入 manifest）。

---

## 2) FND 對照（最小必載）

### 2.1 FND-01（`t_session` 多版本）

| 項目 | 契約 |
|------|------|
| **適用表** | `t_session`（及任何以 `session_id` join 之前置步驟） |
| **行為** | 同一 `session_id` 保留**單一代表列**；排序與 trainer／scorer 對齊：`ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC` 取 `rn=1`（見 FINDINGS、trainer 註解）。 |
| **禁止** | 依賴 `FINAL` 作為去重手段（非決定性）。 |
| **Rule** | `preprocess_session_v1`（manifest 可引用 `preprocessing_rule_id=preprocess_session_v1`）。 |

### 2.2 FND-03（`casino_player_id` 字串髒值）

| 項目 | 契約 |
|------|------|
| **適用** | 含 `casino_player_id` 之表（主要 `t_session`） |
| **行為** | 空字串、純空白、字面值 `'null'`（大小寫不敏感）→ 視為 **NULL**；trim 後再判斷。 |
| **Rule** | `preprocess_session_v1`（若下游身分映射使用該欄，須在本階段完成）。 |

### 2.3 FND-11／FND-12（身分與幽靈人口）

| 項目 | 契約 |
|------|------|
| **FND-11 / canonical eligibility** | 本資產層 **run/trip 主鍵仍為 `player_id`**；但為了及早排除 unwanted players，L1 前須先複製現行 trainer canonical mapping eligibility 語義：建置 `player_id -> canonical_id`，僅保留 `canonical_id` 來自有效 `casino_player_id` 的 rated players；`canonical_id = player_id` 的 unrated / non-rated 觀測不得進 L1 下注流。 |
| **FND-12（定案：強制）** | 在 **FND-01 去重 + FND-04 幽靈 session 排除 + placeholder `player_id` 排除 + rated eligibility** 後之 session 集合上，偵測 dummy／假帳號 `player_id`（FINDINGS **[FND-12]** 與 `trainer/identity.py` 之 `_identify_dummy_player_ids` / `get_dummy_player_ids_from_df` / ClickHouse `_build_dummy_sql` 語義對齊）。產出之 **`dummy_player_id` 集合**須可供 manifest 或 sidecar 引用；**所有進入 L1 下注流／run 物化之 bet** 須排除該等 `player_id`（對齊 trainer **TRN-04**：`trainer/training/trainer.py` 於 chunk 特徵前自 bets 剔除）。 |

### 2.4 Trainer rated-only scope（強制）

| 項目 | 契約 |
|------|------|
| **依據** | `ssot/trainer_plan_ssot.md` 明定本專案僅對 **Rated player** 建模與推論；Non-rated 觀測不參與訓練與推論。 |
| **行為** | `preprocess_session_v1` 須輸出或等價產出 **rated eligibility sidecar**（例如 `eligible_player_ids` / `unrated_player_ids` / `dummy_player_ids`）；`preprocess_bet_v1` 在進 L1 前套用該 eligibility，及早剔除 unrated / dummy / placeholder player。 |
| **邊界** | canonical mapping 只作為 **eligibility filter** 與 sidecar 證據；後續 run/trip ID、分區與 membership 仍以通過 eligibility 後的 `player_id` 為主鍵。 |

### 2.5 FND-13（事件時間 vs 可觀測時間）

| 項目 | 契約 |
|------|------|
| **行為** | **不得**以 `__etl_insert_Dtm` / `__ts_ms` 作為 bet／run 之業務排序主鍵；與 `time_semantics_registry` 之 `event_time_col` / `observed_at_col` 一致。 |
| **backfill 判定** | 使用 `observed_at_col - event_time_col`（通常 `__etl_insert_Dtm - event_time_col`）分析延遲分佈；每表需記錄 p50 / p95 / p99 / max、已知重大回填事件、以及 future breach 的處理策略。 |
| **ingest_delay** | 衍生指標寫入 manifest `ingestion_delay_summary`；threshold policy 於表別分析後決定，本文件不硬編碼數值。 |

---

## 3) `preprocess_bet_v1`（`t_bet`）— 規則條目

以下條目須可在 manifest／稽核 log 中引用 **子規則代碼**（建議欄位 `preprocess_subrule_id` 或併入 `ingestion_fix_rule_id` 命名空間，由 implementation 擇一，**全專案一致**即可）。

| Subrule | 內容 |
|---------|------|
| `BET-PK-01` | **`player_id` 必須有效；`player_id = -1` 一律排除**（placeholder，與 identity／trainer 一致）。 |
| `BET-PK-02` | `bet_id` 非 NULL、唯一（重複版本須先於 L1 前解析為單一列或分流至補丁流程）。 |
| `BET-DQ-01` | 排除 `is_deleted` / `is_canceled`（若來源表具該欄）與協議定義之無效 status（對齊既有 trainer DQ）。 |
| `BET-DQ-02` | **FND-12**：排除屬於 `preprocess_session_v1` 產出之 **dummy `player_id` 集合** 之所有 bet（與 TRN-04 一致）。 |
| `BET-DQ-03` | **Rated-only eligibility**：排除屬於 `preprocess_session_v1` / canonical mapping eligibility 判定為 **unrated / non-rated** 的 `player_id`。此步驟複製現行 trainer player filter 語義，目的為避免在 L1 對非目標玩家做 run/trip 計算。 |
| `BET-OBS-01` | 以 `observed_at_col - event_time_col` 分析 backfill / late arrival；超過表別 threshold policy 時進 correction / 重算流程或留待表別決策，不得 silent pass。 |
| `BET-ORD-01` | 序：`ORDER BY payout_complete_dtm ASC, bet_id ASC`（對齊 implementation plan §4.2）。 |

---

## 4) `preprocess_session_v1`（`t_session`）— 規則條目

| Subrule | 內容 |
|---------|------|
| `SES-DEDUP-01` | FND-01 窗口去重（見 §2.1）。 |
| `SES-DQ-01` | 行為建模路徑：`is_manual = 0`；排除幽靈／無下注 session 之判斷與 **FND-04** 一致（`COALESCE(turnover,0)>0 OR COALESCE(num_games_with_wager,0)>0` 等—與現行 trainer 對齊）。 |
| `SES-DQ-02` | FND-03：`casino_player_id` 清洗。 |
| `SES-DQ-03` | `is_deleted` / `is_canceled` 依協議過濾。 |
| `SES-PK-01` | **`player_id = -1` 不參與** dummy 連結／映射前之有效列集合（與 `get_dummy_player_ids_from_df` 前置 mask 一致）。 |
| `SES-ELIG-01` | 建置 canonical mapping eligibility：保留 `canonical_id` 來自有效 `casino_player_id` 的 rated players；標記 / 輸出 unrated `player_id` 供 `BET-DQ-03` 剔除。 |
| `SES-FND12-01` | **FND-12**：於 §2.3 所述篩後 session 上偵測 dummy `player_id`，輸出 **dummy 集合**（供 BET-DQ-02 與稽核；訓練側已寫 sidecar `dummy_player_ids` 者應可對照）。 |
| `SES-OBS-01` | 使用 `session_end_dtm` 作為本 asset layer registry 之 event time；**`session_end_dtm IS NULL` 之列直接排除**（不進 FND-01 去重後之代表列、不進 eligibility／dummy 計算）；排除列數須在批次 QA 摘要或 manifest 附錄可稽核。 |

---

## 5) Manifest 可引用欄位（對齊）

| Manifest 邏輯欄（SSOT §8） | 與 preprocessing 之關係 |
|---------------------------|-------------------------|
| `preprocessing_rule_id` | 上述 `preprocess_*_v1` 之一。 |
| `preprocessing_rule_version` | 對應本文件 v1／semver。 |
| `ingestion_fix_rule_id` / `ingestion_fix_rule_version` | 若該批次套用 ingestion 列修正（§4.4），與 subrule 或 fix pack 對齊。 |
| `ingestion_delay_summary` | 依 registry `event_time_col` / `observed_at_col` 衍生；**published 路徑強制非空**（見 SSOT／execution plan）。 |
| `eligibility_sidecar`（或等價） | 記錄 rated eligibility / unrated / dummy player 集合之產物路徑或 hash；名稱可由實作決定，但需能重現 `BET-DQ-02` / `BET-DQ-03`。 |

### 5.1 Eligibility sidecar 實體格式（定案：方案 A）

**根路徑**（相對 repo 根）：

`data/layered_assets_sidecar/<source_snapshot_id>/`

**建議檔案（Parquet 優先；窄表、可 DuckDB 直接掃）**：

| 檔名 | 用途 |
|------|------|
| `canonical_eligibility/rated_player_map.parquet` | 至少 `player_id`, `canonical_id`（rated：`canonical_id` 來自有效 `casino_player_id`）；可加 `cutoff_dtm` 或 `as_of` 欄與 trainer 防洩漏語義對齊。 |
| `dummy_player_ids.parquet` | FND-12 dummy `player_id` 清單（可加 `reason` / `rule_version`）。 |
| `excluded_unrated_player_ids.parquet`（可選） | 若審計需要，單獨列出被 `BET-DQ-03` 剔除之 `player_id`；否則可由 map 差集推得，但需文件化。 |
| `build_manifest.json` | 輸入 parquet 路徑、`preprocessing_rule_id`／version、各檔 row_count、輸出檔 hash、DuckDB／腳本版本。 |

**治理**：

- 本 repo 之 `.gitignore` 已忽略整個 **`data/`**，故 **`data/layered_assets_sidecar/` 預設不進版控**（與 `gmwds_*.parquet` 相同策略）。若未來改為部分提交 `data/`，再另加細粒度 ignore 規則即可。
- `manifest.json`（SSOT §8）中 `eligibility_sidecar`（或等價欄位）應指向 **`build_manifest.json` 或上述目錄** 之穩定相對路徑 + content hash。

---

## 6) `preprocess_game_v1`（`t_game`）— 目前政策

- 目前僅套用 **`game_id` 去重底線**（FND-14）與 **observed delay / backfill 分析**（`observed_at_col - event_time_col`）。
- 因尚未完整研究 `t_game`，**不做其他 filtering**，也不得被 feature dependency registry 引用為 L1/L2 特徵來源。
- 待完成 game 表 DQ 與延遲分析後，於本節補 Subrule 表並 bump `preprocessing_rule_version`。

---

## 7) 現行把關流程（repo 實況）：`t_bet` / `t_session` vs 待定的 `t_game`

> 說明：專案內**未**見獨立之「人工作業批核單（ticket）」流程；下列為**實際生效**的工程與文件把關鏈，供你對照後決定 **`t_game` 是否採相同模式或加嚴**。

### 7.1 `t_bet`（下注主線）

| 層級 | 現況 |
|------|------|
| **規格／風險** | `doc/FINDINGS.md`、SSOT／implementation plan、`schema/time_semantics_registry.yaml`、`package/deploy/models/feature_spec.yaml`。 |
| **程式把關** | 訓練 chunk：`trainer/training/trainer.py` 對 bets 之 DQ（含刪除／作廢／狀態等，與計畫對齊處）；依 canonical mapping eligibility 只保留 rated players；**TRN-04** 於特徵前剔除 **FND-12 dummy `player_id`**；服務端 `trainer/serving/scorer.py` 下注查詢與 plan 對齊。 |
| **變更控制** | Git／code review；分層資產 Phase 0 起另以 `doc/time_semantics_registry_pr_checklist.md`、`make check-layered-contracts` 守 registry／契約例。 |
| **進 L1（本產線）** | Phase 1 起由 **`preprocess_bet_v1` + eligibility sidecar + manifest `preprocessing_rule_*`** 留痕；unrated / dummy / placeholder 必須在 L1 前排除。 |

### 7.2 `t_session`（身分／dummy／去重）

| 層級 | 現況 |
|------|------|
| **規格／風險** | FINDINGS **FND-01/02/03/04/12**；`trainer/identity.py`（`get_dummy_player_ids` / `get_dummy_player_ids_from_df`、`build_canonical_links_and_dummy_from_duckdb`）；DECISION_LOG 與 SSOT §5 幽靈 session 語義對齊。 |
| **程式把關** | 訓練：FND-01 CTE 去重、FND-04 mask、placeholder `player_id` 排除後建 canonical mapping eligibility，並做 **FND-12**；`dummy_player_ids` 可落 sidecar（見 trainer 建 sidecar 邏輯）；scorer 使用 **FND-01 + FND-04** 之 session 子查詢。 |
| **變更控制** | 同 bet：**PR + review**；dummy／mapping 行為變更通常需 **DS + Model Owner** 關注（訓練／映射面），但 repo 內無獨立表單。 |
| **進 L1（本產線）** | 對齊 **`preprocess_session_v1`**；rated eligibility、unrated 與 dummy 集合須與 trainer 可重現一致後才得宣告 Gate 通過。 |

### 7.3 `t_game`（供你決定批核）

| 層級 | 現況 |
|------|------|
| **Registry** | `time_semantics_registry` 已載明 **未經核准不得進 L1/L2 特徵**（`preprocessing_contract`）。 |
| **目前政策** | 與其他表同樣做 `observed_at_col - event_time_col` 分析與重大 backfill episode 文件化；除此之外因尚未研究，**不加其他 filtering**。 |
| **若未來要引用 game 特徵** | **DS / Feature Owner**：在 **feature dependency registry**／coverage 標記「依賴 `t_game`」之 `(track_section, feature_id)`；**Model Owner**：核准該等特徵進訓練／線上包；**Data Platform**：bump `definition_version`／寫入 manifest 與本文件 §6 Subrule。 |

---

## 8) Phase 1 實作備忘：記憶體與 eligibility sidecar

- **禁止**：為產出 **eligibility sidecar**（rated / unrated / dummy 等集合）而將 **全量 `t_session` 與 `t_bet`** 以 **pandas 一次載入記憶體**。專案資料量級（數億 bet、數千萬 session）下，筆電與一般單機節點極易 **OOM** 或嚴重拖慢。
- **建議**：以 **DuckDB**（或等價引擎）**掃描分區 Parquet**，在 SQL 內完成 FND-01、FND-04、canonical eligibility、FND-12 dummy 等聚合與篩選，再只 **寫出小體積 sidecar**（例如精簡欄位表、或依設計上限之 `player_id` 清單／Bloom／bitmap，由實作選型但須可審計重現）。
- **分窗／分區**：sidecar 與下游 bet 篩選應對齊同一 **切分鍵**（例如 **`gaming_day`**、`source_snapshot_id`、或固定時間窗），分批產出與 join，避免重複全表掃描與峰值 RAM 疊加。
- **語意不變**：canonical mapping 仍僅為 **L1 前 eligibility gate**；run/trip 物化與 membership 仍以通過 eligibility 後的 **`player_id`** 為主鍵。
