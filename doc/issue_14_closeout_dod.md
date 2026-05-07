# Issue #14 Closeout — DoD 勾選（Implementation Plan v2 §DoD + Workstream A）

對照 [mvp_replace_trainer_implementation_plan_v2_e401ba35.plan.md](C:/Users/longp/.cursor/plans/mvp_replace_trainer_implementation_plan_v2_e401ba35.plan.md) **DoD（Implementation 層）** 與 Workstream A 契約。本檔供關 GitHub issue #14 時貼連結或摘要。

## DoD（v2 原文對照）

- [x] **使用者在同一套既有 CLI 操作下，不需手動執行 `parallel_lda_mvp` 即可完成主要流程**  
  - Trainer local：`run_cross_entry_data_preflight` → `ensure_local_bridge_ready_for_training`（`trainer/training/trainer.py`、`trainer/training/local_bridge_preflight.py`）。  
  - Backtester local：同上（`trainer/training/backtester.py`）。  
  - Scorer/Validator：啟動時 ClickHouse preflight（`trainer/serving/scorer.py`、`trainer/serving/validator.py`）；local 離線推理為後續擴充（v2 WS4 已註明）。

- [x] **當流程慢時，終端可見明確階段訊息與原因（非黑箱等待）**  
  - `AutoBuild[<phase>]:` / `AutoBuild[summary]:`（WS3，`local_bridge_preflight.py`）。  
  - MVP 子行程前有 **high RAM / long runtime** 提示（單測：`tests/unit/test_cross_entry_preflight.py` → `TestWS5ResourceGuardMessages`）。

- [x] **Workstream A 契約（manifest SSOT、`phase_c` 欄位檢查、cache 語意）保持成立，沒有回退到舊猜測式路徑**  
  - `trainer/training/data_sources.py`：`load_local_parquet` 經 manifest、`probe_trainer_local_parquet_bridge_readiness` fail-fast。  
  - 守門測試：`tests/unit/test_workstream_a_bridge_manifest.py`。

- [x] **在筆電資源限制下可完成小樣本 E2E，且建置流程具備 skip/增量行為**  
  - 小樣本：單元 mock autobuild／bridge；`TRAINER_AUTOBUILD_FULL_MVP` 關閉路徑有測。  
  - skip/增量：依 `parallel_lda_mvp` / `trainer_bridge_mvp` 與上述 env；WS5 fast gate 作為筆電可跑回歸。

## WS5 回歸守門

- [x] **Fast gate** — `bash scripts/ws5_regression_gates.sh fast`  
  - 涵蓋：`test_workstream_a_bridge_manifest.py`、`test_cross_entry_preflight.py`、`test_trainer.py::TestRefactorGuardrailsInputSources`。

- [x] **Optional gate 說明** — `bash scripts/ws5_regression_gates.sh optional`（印出手動慢測／全 unit 指令）。

---

## 手動 Smoke（本機執行紀錄）

執行環境：Windows 10，`C:\Users\longp\Patron_Walkaway`，日期以關單當日為準。

| 步驟 | 指令 | 結果 |
|------|------|------|
| 1 | `bash scripts/ws5_regression_gates.sh fast` | **27 passed in ~1.7s** |
| 2 | `python -m trainer.trainer --help` | **OK**（顯示 usage） |
| 3 | `python -m trainer.training.backtester --help` | **OK**（`argparse` 明已改為 ASCII 連字號，避免 Windows cp932 下 `UnicodeEncodeError`）。 |
| 4 | `run_cross_entry_data_preflight(..., use_local_parquet=False)` | **失敗（可接受於本機）**：`clickhouse_connect not available`（未安裝或未載入 `.env`）。錯誤訊息含 **CH_HOST** 等提示，符合可讀性設計。有 ClickHouse 之環境可重跑標 **OK**。 |

---

**關閉 Issue #14**：於 GitHub issue 留言貼上表摘要 + 本檔路徑 `doc/issue_14_closeout_dod.md` + 相關 commit／branch。

---

## GitHub #17 / TRN-17-01：從 raw local parquet 到 L2 訓練（自動 bundle）

- **契約**：[`schema/l2_training_bundle.schema.json`](../schema/l2_training_bundle.schema.json)（`l2_training_bundle.json`）。
- **物化**：`trainer/training/l2_bundle_materialize.py` — 由 chunk Step 7 產物寫入預設目錄 `<repo>/data/l2_training_bundle/`（可用 `--l2-auto-bundle-dir` 覆寫）。
- **Trainer 行為**：`--use-local-parquet` **且**加 `--l2-auto-from-local`，並**未**加 `--legacy-chunk-mode`、**未**指定 `--l2-training-bundle`、**未**加 `--no-l2-auto-bundle` 時：
  1. Step 3 後若快取命中（bridge manifest stat + 視窗 + `recent_chunks` + split 比例 + feature spec fingerprint + canonical rebuild 旗標等），直接走 L2 Steps 8–10。
  2. 否則跑完 Step 7 後物化 bundle，再跑 L2 Steps 8–10（跳過 chunk Step 8–10）。
- **相容**：僅 `--use-local-parquet` 而**未**加 `--l2-auto-from-local` 時，維持完整 chunk Step 4–10（避免舊測試／腳本在未宣告意圖時誤入 L2 路徑）。
- **關閉舊行為**：需要完整 chunk 管線時加 **`--legacy-chunk-mode`**。
- **bridge `source_snapshot_id`**：寫入 bundle manifest；若缺 bridge manifest 則使用占位 `local_parquet_no_bridge_manifest`（建議仍維持 Issue #14 bridge 產物以利 lineage）。
