# Chunk cache：可攜式命中與兩階段快取（修訂計畫）

> **狀態**：草案（2026-04-07 修訂；2026-04-07 審查意見併入）  
> **範圍**：`trainer/training/trainer.py` 之 Step 6 chunk cache（`process_chunk`、`.cache_key`、可選 `*.prefeatures.*`）。  
> **非範圍**：Step 7 DuckDB 暫存目錄（pid 隔離）、`player_profile` ETL 本體（僅文件交叉引用）。

---

## 1. 目標

1. **可攜性**：同一批 `data/gmwds_t_*.parquet` 複製到另一台機器或另一路徑後，在相同訓練邊界與設定下應能 **cache hit**（不因 `mtime` 變更而全 miss）。
2. **同機迭代**：在只改 `feature_spec.yaml` 或 `neg_sample_frac` 等常見情境下，盡量 **少重算 Track Human**（最貴的純 CPU 段之一）。
3. **正確性優先**：寧可 false miss（多算一次），避免 **silent false hit**（用錯輸入訓練）。

---

## 2. 現況與問題

| 項目 | 說明 |
|------|------|
| Local `data_hash` | `_local_parquet_source_data_hash` 含 `st_mtime_ns` → 複製檔案常導致 key 變動、全 miss。 |
| ClickHouse 路徑 | 已用 raw bets 的 order-insensitive digest；與本計畫無衝突。 |
| Two-stage cache | `CHUNK_TWO_STAGE_CACHE` 預設關；改 spec / neg 時易連 Track Human 一起重算。 |
| Final chunk key | 含 `feature_spec_hash`、`neg_sample_frac`、`profile_hash` 等屬合理；問題主要在 local 來源指紋與兩階段未預設啟用。 |

---

## 3. 修訂後的階段（精簡版）

### Phase B1 — 兩階段 chunk 快取（**建議第一順位**）

- **作法**：將 `CHUNK_TWO_STAGE_CACHE` 改為 **預設開啟**，或新增 **`trainer/core/config.py` 單一 SSOT 常數**（符合「設定集中於 config」），預設為 on；**仍保留** `CHUNK_TWO_STAGE_CACHE=0`／`false` 等 **env 覆寫**（與現有 opt-in 行為相容、便於 CI／筆電關閉）。
- **效果**：`*.prefeatures.parquet` 可在 **spec / neg_sample_frac 變更**時仍命中，跳過 `add_track_human_features`。
- **成本**：磁碟多存一份 prefeatures parquet／sidecar；需在 README 或本文件註明清理方式。
- **已知風險（Task 7 R6 Code Review 已記）**：
  - **OOM**：prefeatures **hit** 路徑為 `pd.read_parquet(prefeatures)` **整表載入**，與未啟用 R6 時峰值 RAM 同量級；多 chunk 並行時風險未自動降低。
  - **雙寫**：miss 後會同時寫 `*.prefeatures.parquet` 與最終 `chunk_*.parquet`，磁碟寫入與空間約 **2×**（相對僅寫最終 chunk）。
- **文件／產品約束**：預設開啟時應在 runbook 註明 **建議單 chunk 串行、足夠 RAM**；磁碟緊張環境可關閉或僅讀不寫（若日後實作 read/write 分離）。
- **驗收**：同資料第二趟只改 YAML 註解以外的 `track_llm` 相關段落時，log 出現 **prefeatures hit**；或改 neg 時至少 prefeatures 層可 hit（依 `_prefeatures_cache_components` 設計）。

### Phase A — 可攜式 local 來源指紋（**第二順位，核心修復**）

- **作法**：重寫 `_local_parquet_source_data_hash` 的 `_file_token`，**移除 `st_mtime_ns`**，改為至少包含：
  - 檔案 **size**（bytes）、
  - Parquet **num_rows**（footer metadata）、
  - **穩定 metadata digest**（須在實作前定稿欄位子集，避免實作當下才決定而失衡 false hit／false miss）：
    - **須排除**或正規化易變、與資料內容無關的欄位，例如 **`created_by`**（含 PyArrow 版本字串）、**pandas metadata** 等，否則 **跨機器 PyArrow 版本不同** 會導致 **false miss**。
    - **建議納入**與列資料與 schema 相關且相對穩定的訊號，例如各 **row group** 的 **`total_byte_size` / `num_rows`**、**column 名稱與型別**（canonical 排序後 digest）；若採「footer bytes hash」須確認是否含上述可變欄位並相應剔除。
  - 仍須 **零資料列掃描**；以 PyArrow `pq.read_metadata` 等可用 API 為準。
- **保留**：filter bounds（`bet_filter_*`、`sess_filter_*`）仍在 payload 內，避免換時間窗仍誤 hit。
- **可選強化**：在 sidecar JSON（或 fingerprint 附帶欄位）增加 **`fingerprint_version`**（或 `data_hash_algo`），便於日後升級／降級演算法時辨識舊 key、避免除錯時混淆；rollback 舊版程式時會再觸發一輪全 miss，屬可接受成本。
- **驗收**：
  - 兩份 **位元組相同**、僅 **mtime** 不同的 parquet → **同一 `data_hash`**。
  - **貼近實務**：經 **`cp` / `scp` / 複製到另一路徑** 後，在相同 bounds 與設定下 **`data_hash` 與複製前一致**（size、num_rows、選定 digest 不變）。
  - 變更 bounds → key 變。

### Phase B2 —「語義」feature spec hash（**可選／延後**）

- **預設不做**：自行定義「語義子樹」易漏欄位 → **silent stale cache** 風險高於 false miss。
- **若未來要做**：先量測「整檔 MD5 因無關編輯導致 miss」的頻率；再評估「YAML canonical 化後 hash」等低風險方案，並補契約測試。

### ~~Phase C（pre-neg-sample 第三層）~~ — **不採納**

- **理由**：`NEG_SAMPLE_FRAC_AUTO` 會依 **可用 RAM** 調整 effective frac → 跨機器本來就難與「可攜快取」並存；維護第三層成本高。兩階段（B1）已涵蓋「改 neg 仍可能重用 prefeatures」的主要效益。

### Phase D — 文件與搬移 checklist（**僅文件，不改程式**）

- 說明需一併複製的產物：`CHUNK_DIR/chunk_*.parquet`、`chunk_*.cache_key`、（若啟用）`chunk_*.prefeatures.parquet` 與對應 `.prefeatures.cache_key`。
- 註明 `DATA_DIR`／repo `data/` 與訓練邊界需一致。

---

## 4. 向後相容與首次部署

- 變更 `data_hash` 演算法後，既有 `.cache_key` 內 fingerprint **必然與新 key 不一致** → **首次 run 全 miss、重算 Step 6**。屬預期行為，應在 STATUS／release note 註明。
- **兩層快取一併失效**：`data_hash` 參與 **最終 chunk key** 與 **prefeatures key**（`_prefeatures_cache_components` 沿用同一 `components`）。若 **B1 已上線** 且磁碟上已有舊格式 prefeatures，**Phase A 上線後** 會同時 **final chunk miss** 與 **prefeatures miss** → 首次 run 可能 **比「只動一層」更慢**（兩層皆需重寫）；正確性無虞，文件應寫清楚。
- **Rollback**：若新指紋需緊急退回舊版（例如重新依賴 `mtime`），會再觸發一輪全 miss；`fingerprint_version` 可減輕除錯成本。
- **不強制** sidecar 欄位遷移腳本；若未來要軟遷移，可另開任務（讀舊 JSON → 僅比對仍相容的欄位）。

---

## 5. 觀測與驗收（共用）

- 沿用 `pipeline_diagnostics.json` 之 `step6_chunk_cache_*` 與 log 內 **`miss_reason`**。
- **指紋差異**：既有 `_parse_chunk_cache_fingerprint_pipe` 等路徑可對 **window / data_hash / cfg / profile / spec / neg** 做逐段比對；Phase A／B1 實作時 **沿用並擴充**（若新增 sidecar 欄位或 fingerprint 格式）即可，無需另起一套觀測。
- 建議手動或 CI 記一筆：**同資料第二趟 Step 6 `step6_duration_sec`** 與 hit 計數對照。

---

## 6. 建議實作順序

1. **B1**：預設（或 config + env 覆寫）開啟兩階段 chunk cache，並補 runbook 的 RAM／磁碟注意事項。  
2. **A**：local 來源指紋移除 mtime + **事先定稿**之 metadata digest 子集 + 可選 `fingerprint_version`。  
3. **D**：補 `.cursor/plans` 或 `doc/` 搬移 checklist（若已有長文可只加交叉連結）。  
4. **B2**：僅在有效益證據後再做。

---

## 7. 相關程式與測試（實作時）

- **程式**：`trainer/training/trainer.py` — `_local_parquet_source_data_hash`、`_chunk_two_stage_cache_enabled`、`process_chunk`、`_write_chunk_cache_sidecar`、`_prefeatures_cache_components`、sidecar 讀寫。
- **設定**：`trainer/core/config.py`（SSOT 常數；邏輯內勿散落魔術字串）。
- **測試（Phase A／B1 變更後須對齊更新）**：
  - `tests/unit/test_task7_chunk_cache_key.py`
  - `tests/review_risks/test_task7_r5_local_metadata_review_risks_mre.py`（local metadata／指紋語意）
  - 若動到 R6：`tests/review_risks/test_task7_r6_prefeatures_review_risks_mre.py`  
  **注意**：workspace 規則規定不可隨意改動 `tests/` 內檔案，**除非**既有測試因契約變更而錯誤 — 本計畫變更指紋與預設開關時，**預期需更新上述單元／review 測試**（屬契約變更，非「順手重構」）。

---

## 8. 風險摘要

| 風險 | 緩解 |
|------|------|
| Footer／metadata digest 仍無法偵測極端 in-place 竄改 | 接受或提供可選「全檔 hash」模式（慢，僅驗證用）。 |
| **PyArrow／Parquet metadata 含版本或可變欄位** → 跨機 **false miss** | digest **排除** `created_by`、pandas 專用 metadata 等；只取 row group 統計與 schema 等穩定子集。 |
| **B1 預設開啟後 RAM 峰值**（prefeatures 整表 `read_parquet`） | Runbook 註明 **單 chunk、足夠 RAM**；保留 env／config **關閉**路徑。 |
| 兩階段快取磁碟膨脹、雙寫 | 文件註明清理與磁碟預算；必要時日後 **read/write 分離**（見 STATUS R6 review）。 |
| B2 語義 hash 漏欄 | 預設不做 B2。 |
