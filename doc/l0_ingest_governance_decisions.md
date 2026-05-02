# L0 ingest — 治理決策（已定案 + 待決）

> **對齊**：`doc/l0_layered_data_assets_convention.md` §4、`scripts/l0_ingest.py`、`layered_data_assets/l0_fingerprint.py`。

---

## 已定案

### `source_snapshot_id`：**策略 A**

- **Id 完全由指紋決定**：省略 `--snapshot-id`；`snap_<32hex>` 由 `snapshot_fingerprint.json` 的 canonical JSON 做 SHA-256 後截斷得出。
- **不**在正式批次使用 `--allow-snapshot-id-mismatch`（僅除錯／遷移腳本可斟酌使用）。

### 指紋裡的路徑：**一律相對路徑**

- 所有 `--source` 必須在 **`--anchor-path`（預設 repo 根）底下**可 `relative_to`；否則 ingest **失敗**（不再寫入絕對路徑）。
- 跨機器重現時，只要 **anchor 以下相對路徑 + 檔案位元組內容** 相同，即得到相同 `source_snapshot_id`。

### `data/` 與版控：**大型 Parquet 不進 git**

- `.gitignore` 使用 **`data/**/*.parquet`**：忽略 `data/` 下所有 Parquet（含 L0 `part-*.parquet`、sidecar 等），**目錄與非 parquet 檔**（例如 `.gitkeep`、`snapshot_fingerprint.json`、小型 JSON）仍可納版控。
- CI 仍**不**依賴大型真資料；僅跑契約與小檔單元測試。

### Manifest `source_hashes` 與 fingerprint（LDA-E1-06）

- **寫入時**：各 L1 批次腳本（preprocess、`run_fact`／`run_bet_map`／`run_day_bridge`）仍以既有 `source_partitions` 為準；若提供 `--l0-fingerprint-json`，於寫 manifest 前以 `layered_data_assets.manifest_lineage_v1.merge_source_hashes_into_manifest` 將 `snapshot_fingerprint.json` 之 `inputs[*].sha256` 轉成 `sha256:<hex>`，並**依 `len(source_partitions)` 截斷或重複第一個 hash 補齊長度**（MVP：多分區 lineage 仍為單一 L0 指紋時之穩定形狀）。
- **後補／審計**：`scripts/manifest_lineage_preview_v1.py` 可對既有 `manifest.json` 僅刷新 `source_hashes` 與／或自指定 Parquet 重算 `ingestion_delay_summary`（預演，與批次寫入同一套計算）。

---

## `ingest_recipe_version` 是什麼？何時必須 bump？

### 它是什麼

`ingest_recipe_version` 是寫進 `snapshot_fingerprint.json` 頂層的一個**短字串**（預設 `l0_ingest_v1`），代表：

> 「在**輸入檔路徑與 SHA-256 都相同**的前提下，L0 ingest **如何把這些檔變成快照目錄**」的**語意版本**。

指紋裡已含每個來源檔的 `sha256` 與 `size_bytes`，因此「只換 export 檔內容、不改 ingest 行為」時，**不必**手動 bump：`source_snapshot_id` 會因 `sha256` 變化而自動變新。

但它**不含**下列若未反映在「來源檔內容」上的變化，例如：

- 你仍讀**同一個**上游 Parquet（位元組不變），但 ingest 改為「只複製其中部分列／改檔名規則／改 Hive 目錄慣例」；
- 或指紋 JSON **結構**變更（增刪欄位名、改 `layout` 意義）而舊批次仍宣稱同一 `ingest_recipe_version`。

此時若**不** bump `ingest_recipe_version`，可能出現：**不同語意的兩次產線**卻得到**相同** `source_snapshot_id`（因為進入 canonical JSON 的 bytes 與字串仍舊），違反「一批次一語意」的稽核預期。

### 何時 bump（建議規則）

| 情況 | 是否 bump `ingest_recipe_version` |
|------|-----------------------------------|
| 僅更換來源檔內容（或增刪來源檔），`scripts/l0_ingest.py` 的 materialize 語意不變 | **否**（id 由 inputs 的 hash／集合自然變） |
| 修改 `l0_ingest` 的複製／命名／分區邏輯，且**未**改變上游來源檔內容 | **是** |
| 指紋 JSON schema（欄位名或 `layout` 語意）變更 | **是** |
| 與 `doc/preprocessing_layered_data_assets_v1.md` 約定之 **preprocess 語意**對齊需要（例如正式宣告「自 v2 起 L0 定義變更」） | **是**（建議字串與該文件版次可對照，例如 `l0_ingest_v1_1` 或 `l0_ingest_v2`） |

### 維護者（單人 repo）

- **本 repo 僅一人維護**：凡變更 `scripts/l0_ingest.py`、指紋 JSON 結構或 L0 materialize 語意者，由 **maintainer 在同一變更裡 bump `ingest_recipe_version`（若觸發上表）**，並在 commit／PR 說明簡述原因即可。
- 若日後擴編為多人團隊，再恢復「角色分工 + review 檢查 bump」即可。

---

## 仍待決（可後補）

### 排程與執行環境

觸發頻率、是否允許自動 `--force`、大檔是否改 hardlink／artifact store——需要時再補一段運維備忘即可。

---

### 附：策略 B（人類可讀 id）

正式批次**不採用**；若未來審計工具需要，再於 SSOT 增列例外流程。
