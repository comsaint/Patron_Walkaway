# Chunk cache：可攜式命中與兩階段快取（修訂計畫）

> **狀態**：草案（2026-04-07 修訂）  
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

- **作法**：將 `CHUNK_TWO_STAGE_CACHE` 改為 **預設開啟**，或新增 `config/` 單一 SSOT 開關（符合「設定集中於 config」專案規範），預設為 on。
- **效果**：`*.prefeatures.parquet` 可在 **spec / neg_sample_frac 變更**時仍命中，跳過 `add_track_human_features`。
- **成本**：磁碟多存一份 prefeatures parquet／sidecar；需在 README 或本文件註明清理方式。
- **驗收**：同資料第二趟只改 YAML 註解以外的 `track_llm` 相關段落時，log 出現 prefeatures hit；或改 neg 時至少 prefeatures 層可 hit（依 key 設計）。

### Phase A — 可攜式 local 來源指紋（**第二順位，核心修復**）

- **作法**：重寫 `_local_parquet_source_data_hash` 的 `_file_token`，**移除 `st_mtime_ns`**，改為至少包含：
  - 檔案 **size**（bytes）、
  - Parquet **num_rows**（footer metadata）、
  - **footer / metadata 穩定 digest**（例如讀取 Parquet footer bytes 或 serialised metadata 的短 hash），以降低「row count + size 相同但內容已變」的 false hit 風險。  
  具體實作以 PyArrow 可用 API 為準（須零資料列掃描）。
- **保留**：filter bounds（`bet_filter_*`、`sess_filter_*`）仍在 payload 內，避免換時間窗仍誤 hit。
- **驗收**：兩份位元組相同、僅 mtime 不同的 parquet → **同一 `data_hash`**；變更 bounds → key 變。

### Phase B2 —「語義」feature spec hash（**可選／延後**）

- **預設不做**：自行定義「語義子樹」易漏欄位 → **silent stale cache** 風險高於 false miss。
- **若未來要做**：先量測「整檔 MD5 因無關編輯導致 miss」的頻率；再評估「YAML canonical 化後 hash」等低風險方案，並補契約測試。

### ~~Phase C（pre-neg-sample 第三層）~~ — **不採納**

- **理由**：`NEG_SAMPLE_FRAC_AUTO` 會依 **可用 RAM** 調整 effective frac → 跨機器本來就難與「可攜快取」並存；維護第三層成本高。兩階段（B1）已涵蓋「改 neg 仍可能重用 prefeatures」的主要效益。

### Phase D — 文件與搬移 checklist（**僅文件，不改程式**）

- 說明需一併複製的產物：`CHUNK_DIR/chunk_*.parquet`、`chunk_*.cache_key`、（若啟用）`chunk_*.prefeatures.parquet` 與對應 `.cache_key`。
- 註明 `DATA_DIR`／repo `data/` 與訓練邊界需一致。

---

## 4. 向後相容與首次部署

- 變更 `data_hash` 演算法後，既有 `.cache_key` 內 fingerprint **必然與新 key 不一致** → **首次 run 全 miss、重算 Step 6**。屬預期行為，應在 STATUS／release note 註明。
- **不強制** sidecar 版本遷移；若未來要軟遷移，可另開任務（讀舊 JSON → 僅比對仍相容的欄位）。

---

## 5. 觀測與驗收（共用）

- 沿用 `pipeline_diagnostics.json` 之 `step6_chunk_cache_*` 與 log 內 `miss_reason`。
- 建議手動或 CI 記一筆：**同資料第二趟 Step 6 `step6_duration_sec`** 與 hit 計數對照。

---

## 6. 建議實作順序

1. **B1**：預設（或 config）開啟兩階段 chunk cache。  
2. **A**：local 來源指紋移除 mtime + 強化 footer/metadata digest。  
3. **D**：補 `.cursor/plans` 或 `doc/` 搬移 checklist（若已有長文可只加交叉連結）。  
4. **B2**：僅在有效益證據後再做。

---

## 7. 相關程式位置（實作時）

- `trainer/training/trainer.py`：`_local_parquet_source_data_hash`、`_chunk_two_stage_cache_enabled`、`process_chunk`、`_write_chunk_cache_sidecar`。
- 設定：`trainer/core/config.py`（若新增 SSOT 常數，勿硬寫在 trainer 核心邏輯散處）。

---

## 8. 風險摘要

| 風險 | 緩解 |
|------|------|
| Footer-only digest 仍無法偵測極端 in-place 竄改 | 接受或提供可選「全檔 hash」模式（慢，僅驗證用）。 |
| 兩階段快取磁碟膨脹 | 文件註明清理；必要時保留 env 關閉路徑。 |
| B2 語義 hash 漏欄 | 預設不做 B2。 |
