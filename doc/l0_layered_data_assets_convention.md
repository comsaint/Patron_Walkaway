# L0 目錄與 `source_snapshot_id` 規約（分層資料資產）

> **對齊**：`ssot/layered_data_assets_run_trip_ssot.md` §4.1 L0；`implementation plan/layered_data_assets_run_trip_execution_plan.md` **LDA-E1-01**。  
> **程式輔助**：`layered_data_assets/l0_paths.py`（路徑組裝與 `source_snapshot_id` 格式驗證）。

---

## 1) 實體根目錄

預設相對 repo 根：

`data/l0_layered/`

（**版控策略**：`.gitignore` 使用 `data/**/*.parquet`，大型 Parquet 不進 git，**目錄結構與非 parquet 檔**可保留於版控。正式大量資料仍以 artifact store／管線輸出為準。規約與已定案治理見本檔、`schema/examples/`、`doc/l0_ingest_governance_decisions.md`。）

**Ingest**：`python scripts/l0_ingest.py --help`（寫入 `snapshot_fingerprint.json`、Hive-style 分區目錄；指紋格式見 `schema/examples/snapshot_fingerprint.example.json`）。

---

## 2) 快照目錄（immutable batch）

每一個不可變 L0 批次一個目錄：

`data/l0_layered/<source_snapshot_id>/`

### 2.1 `source_snapshot_id` 格式（機器可驗證）

- **Prefix**：固定 `snap_`。
- **Body**：僅允許 `[A-Za-z0-9_-]`，長度 **8–120**（含 body，不含 `snap_` 時總長需 ≤ 128 字元級實務上限）。
- **禁止**：`..`、`/`、`\`、空白；避免與檔案系統混淆。
- **語意**：同一組「輸入指紋 + ingest 配方版本」重跑必須得到**相同** `source_snapshot_id`（見 §4）。

範例：`snap_a1b2c3d4`、`snap_20260502_trainexport_v3`。

---

## 3) 表與分區（Hive-style）

在快照目錄下依來源表與分區鍵組織（與既有 Parquet 慣例對齊）：

```text
data/l0_layered/<source_snapshot_id>/
  t_bet/
    gaming_day=2026-04-01/
      part-000.parquet
    gaming_day=2026-04-02/
      part-000.parquet
  t_session/
    gaming_day=2026-04-01/
      part-000.parquet
```

- **檔名**：`part-<zero_padded>.parquet` 或單檔 `data.parquet`（實作擇一，**同一快照內一致**即可）。
- **不可變**：寫入後不覆寫；修正走**新快照**或新分區目錄（對齊 SSOT）。

---

## 4) 可重現性（與 manifest `source_hashes` 對齊）

建議 ingest 在產生 `source_snapshot_id` 時，輸入包含：

1. **每個上游檔或分區** 的 **SHA-256**（或專案約定之 hash）；  
2. **ingest 配方**：腳本版本、row filter（若有）、欄位投影清單；  
3. 將 (1)(2) **canonical JSON** 後再 hash → 嵌入 `source_snapshot_id` body，或另寫 `snapshot_fingerprint.json` 於快照根目錄。

`manifest.json` 之 `source_partitions` / `source_hashes` 必須能指回本快照內對應子路徑（見 `schema/manifest_layered_data_assets.schema.json`）。

---

## 5) 與 sidecar 的關係

`data/layered_assets_sidecar/<source_snapshot_id>/`（見 `doc/preprocessing_layered_data_assets_v1.md` §5.1）之 **`source_snapshot_id` 字串規則與本文件相同**，便於批次與 eligibility 產物一對一對齊。

---

## 6) 範例（僅目錄樹，不含大檔）

見 `schema/examples/l0_snapshot_layout.example.txt`（若未產生，可手動依 §3 建立空目錄驗證路徑）。
