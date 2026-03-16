# Tests 目錄說明

本目錄採**分層結構**，便於依類型執行與維護；`pytest tests/` 會遞迴收集所有子目錄，行為與搬移前一致。

## 目錄用途

| 目錄 | 用途 | 建議指令 |
|------|------|----------|
| **unit/** | 純單元測試：不依賴 DB、Parquet、多模組協作；可 mock 或無 I/O。 | `pytest tests/unit/` |
| **integration/** | 整合測試：需 DB、Parquet、trainer/backtester/scorer 流程或跨模組。 | `pytest tests/integration/` |
| **review_risks/** | Code Review／round 回歸：`test_review_risks_*` 與 `test_*_review_risks_*`。 | `pytest tests/review_risks/` |

## 常用指令

- **全量**：`pytest tests/`（等同於搬移前，遞迴所有子目錄）
- **僅單元**：`pytest tests/unit/`
- **僅整合**：`pytest tests/integration/`
- **僅 review_risks**：`pytest tests/review_risks/`

約定：round 編號與 Code Review 風險回歸測試皆置於 `review_risks/`，方便篩選與維護。
