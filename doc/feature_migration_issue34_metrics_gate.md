# Issue #34 — 指標驗證與上線門檻（Metrics gate）

本文件對應「Run/Trip/Player-first、去分鐘窗、移除 t_game」合併後的 **離線驗證清單**。訓練產出仍以 bundle 內 `feature_spec.yaml` 為準；本清單用於 PR / 發版前人工或自動報表對照。

## 1. 資料與契約

- [ ] 訓練矩陣欄位集與 bundle `feature_list.json` / `feature_spec.yaml` 完全一致（無 silent 缺欄）。
- [ ] `join_t_game_features_for_bets` 相關欄位 **不得** 出現在 required 特徵或 scorer 輸入路徑。
- [ ] `track_llm` 候選中 **無** `RANGE … INTERVAL … MINUTE` 視窗（靜態：`tests/unit/test_feature_spec_yaml.py::TestNoClockMinuteWindowsInTemplate`）。

## 2. 指標對照（相對於遷移前基線）

建議至少記錄下列指標於同一 holdout / backtest 窗口（或固定 seed 的小樣本重播）：

| 指標 | 目的 | 備註 |
|------|------|------|
| PR-AUC / AP | 整體排序能力 | 分鐘窗移除後常見略降 |
| Precision@K 或業務門檻點之 Precision | 警報品質 | 與 alert volume 一併看 |
| Alert volume / hour | 業務可承受量 | 語意變更後分佈可能偏移 |
| Calibration（若已做） | 機率可信度 | 非必須 |

**Rollback 建議**：若 PR-AUC 下降超過團隊約定門檻，優先檢查 (1) run 欄位是否全路徑可用 (2) screening 是否仍引用已刪欄位 (3) 舊 bundle 快取是否汙染。

## 3. 效能 / 記憶體

- [ ] DuckDB `track_llm` 批次：仍以 `window_partition_by` 增加 `(canonical_id, run_id)` 分區；大表訓練時觀察 Step 6 耗時與峰值 RAM。
- [ ] 無需載入 `gmwds_t_game.parquet` 時，確認 I/O 與臨時檔下降（可選 log 對照）。

## 4. 簽核

- 產品 / 建模：指標門檻與 alert 策略  
- 工程：train/serve 契約與 bundle hash  

---

*最後更新：對齊 GitHub #34 子議題與 `trainer/feature_spec/feature_candidates.yaml` v0.2 語意。*
