# Polars Pilot Expansion Governance

本文件對應 `polars-baseline-introduction` 計畫中的後兩項待辦：
- trainer 主線擴張候選（純 pandas 路徑）
- baseline pilot 後的 go / no-go 準則

## 下一批候選（純 pandas）

優先順序（高 -> 中）：

1. `trainer/core/schema_io.py::normalize_bets_sessions`
2. `trainer/features/features.py::coerce_feature_dtypes`
3. `trainer/training/trainer.py::compute_sample_weights`
4. `trainer/training/trainer.py::load_player_profile`
5. `trainer/training/trainer.py::load_local_parquet`

這些候選共同條件：
- 屬於 pandas-heavy 純表運算。
- 與 DuckDB out-of-core 主路徑分工清楚。
- 可維持既有函式輸出契約，不強迫全鏈路型別重構。

## 明確非範圍（第一波不碰）

- `trainer/features/features.py::compute_track_llm_features`
- `trainer/features/features.py::compute_column_std_duckdb`
- `trainer/features/features.py::compute_correlation_matrix_duckdb`
- `trainer/training/trainer.py` Step 7 / canonical mapping DuckDB 路徑
- `trainer/labels.py::compute_labels`
- `trainer/training/trainer.py::apply_dq`

原因：
- DuckDB runtime / out-of-core 責任已明確，避免混入第二個大型引擎造成可觀測性退化。
- 上述函式含時區、排序、censoring、train-serve parity 等高風險契約。

## Go / No-Go 準則

## Go

同時滿足以下條件才擴張：

1. baseline pilot 的 `feature_views` 專屬單元測試全數通過。
2. `tests/unit/test_baseline_models_smoke.py` 無回歸。
3. 代表性輸入下，wall time 或 peak RSS 至少一項有可重現改善。
4. 不新增下游轉換噪音（不需要在 runner / 模型層大量補 `.to_pandas()`）。
5. 契約保持一致：欄位順序、列順序、`KeyError` / `ValueError` 行為、`np.float64` 輸出。

## No-Go

任一情況發生即不擴張，停留在 baseline pilot：

1. 只有「可執行」但沒有明顯效能或記憶體收益。
2. 造成 cache、排序、label 對齊、或 train-serve parity 契約漂移。
3. 需改動 DuckDB 主導路徑才能維持穩定，代表分工邊界不清楚。
