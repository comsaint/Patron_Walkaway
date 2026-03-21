# 離線 Holdout 調查計畫（Parquet-only，無 Production ClickHouse）

> **目的**：在無法連線 production ClickHouse 時，以「訓練資料截止至某時點（例如 2 月底）＋本地 Parquet 的後續區間（例如 3 月）」做時間外推評估，補強 test vs production 調查中 **R5／R7** 與部分 **R1／R6** 的離線證據。
>
> **相關文件**：`INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md`（主調查計畫 R1–R9）；調查資產目錄 `investigations/test_vs_production/`。

---

## 定位與限制（必讀）

本計畫是 **Parquet 管線下的時間 holdout**，**不是**完整複製 production：

- **可支持**：時間／分佈漂移（**R5**）、多時間窗代表性（**R7**）、固定閾值在「未來區間」的行為（**R1／R6** 的離線類比）、與 `labels.py` 一致的 label 自洽檢查（**R3** 部分）。
- **不可單憑本計畫結案**：train vs serve 資料路徑與快取語意（**R4**）、ClickHouse 與 Parquet 時間戳鏈（**R9**）、真實 `prediction_log`／`alerts` duplicate suppression 口徑（**R2** 線上段）。

結論與簡報中應明確標註 **資料來源 = 本地 Parquet** 與 **metric 定義**（見下「指標口徑」）。

---

## 階段 0：凍結實驗定義

在開始訓練或評估前，將下列內容寫入 `investigations/test_vs_production/analysis/offline_holdout_<label>/notes.md`（`<label>` 例如 `mar2026`，依實際資料調整）。

1. **資料真實範圍**：以查詢或腳本確認 Parquet 內實際最大／最小 `payout_complete_dtm`（或專案用於切窗的欄位），避免口頭「到 3 月初」與檔案不一致。
2. **時間切分**（範例，按資料調整）  
   - **Train／valid／test（訓練 run 內部）**：僅使用 **≤ train_cutoff**（例如 2026-02-28 或實際最後完整訓練日）。  
   - **Holdout（pseudo–production）**：**> train_cutoff 且 ≤ parquet 實際最大時間**，例如整段 3 月或「3/1～資料最後一日」。
3. **Config parity**：記錄本次使用的環境／config 中與 label、horizon、delay、**`extended_end`／lookahead** 相關項（與 production 意圖一致者以 sanitized 摘要或複製貼上保存）。離線標註必須與訓練使用同一套 `trainer/labels.py` 語意（見主計畫附錄）。
4. **資源（筆電）**：優先使用 `python -m trainer.trainer ... --no-preload`；必要時 `--sample-rated N` 先做小窗 smoke，再擴窗（見專案根目錄 `README.md`）。

---

## 階段 1：建立「train_cutoff 前」模型產物

### 路徑 A：完整重訓

在專案根目錄執行（範例）：

```bash
python -m trainer.trainer --use-local-parquet --start <train_start> --end <train_cutoff_date> --no-preload
```

視需要加上：`--recent-chunks N`、`--skip-optuna`（加速）、`--rebuild-canonical-mapping`（僅在需刻意重算 mapping 時）。

**Canonical／cutoff（對應 R4）**：若未重建，須注意既有 `data/canonical_mapping.parquet` 與 `canonical_mapping.cutoff.json` 的 `cutoff_dtm` 與本次 `train_end` 關係；見根目錄 `README.md`「Canonical mapping 共用 artifact」一節。

### 路徑 B：沿用既有 artifact

若已有對應訓練視窗的模型目錄，可跳過重訓，但在 `notes.md` 中記錄 **model 路徑、model_version、threshold、是否 uncalibrated**（對應主計畫 **R8**）。

### 產出

固定本次使用的 **`--model-dir`** 或 `trainer/models/` 下 run 目錄、artifact 閾值、特徵清單；階段 2 僅使用該產物做 holdout 評估，不再改閾值（除非實驗設計明訂「重選閾」子實驗）。

---

## 階段 2：Holdout 區間評估（與 test 同口徑）

1. **評估入口**：優先使用 `trainer.training.backtester`（`python -m trainer.backtester`，見 `README.md`）在 **固定模型** 上對 holdout 時間窗評分並產出指標。若現有 backtester 會觸發重訓或無法載入 frozen 模型，則需另增「僅 forward + 同口徑指標」的腳本（實作時應小步、可審計）。
2. **指標口徑（對應 R2）**——至少同時記錄：  
   - **固定 artifact 閾值**下的 precision 與 recall；  
   - **precision@recall=1%**（與訓練報告 `test_precision_at_recall_0.01` 同定義）。  
   避免只報其中一種後與 production 儀表板直接混比。
3. **多子窗（對應 R7）**：將 holdout 再切為數個 **6 小時或數日**子窗，報告 precision@recall=1% 的 **分佈或變異**；變異大則結論中註明「單一視窗不具代表性」。
4. **Label**：holdout 樣本的真值一律以 **`trainer/labels.py` 的 `compute_labels()`** 與訓練相同 config 計算（**R3**）；邊界／censored 樣本可記錄筆數占比，不強求與 validator 線上對拍（無 ClickHouse 時無法完成線上段）。

---

## 階段 3（可選）：R4 敏感度分析（仍為離線）

在不連 ClickHouse 的前提下，可二選一或並行（能跑則跑）：

1. **Canonical mapping**：**凍結**訓練結束時的 `canonical_mapping.parquet` + `cutoff.json` 評 holdout，對照「以 train_cutoff 為界更新／重建 mapping 後」再評一次，觀察指標差異是否支持「mapping 過期」假說。  
2. **Profile as-of**：若評分路徑可切換「嚴格 PIT／as-of」與「誤用較新 snapshot」兩種模式，各跑一次作上下界直覺（**非** production 真值）。

---

## 階段 4：與 investigation workspace 對接

| 動作 | 位置 |
|------|------|
| 實驗筆記、指令、指標表、限制聲明 | `investigations/test_vs_production/analysis/offline_holdout_<label>/` |
| CSV、圖、輔助輸出（僅新增） | `investigations/test_vs_production/snapshots/offline_holdout_<YYYYMMDD>/` |
| 更新主調查狀態 | `INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md` §5：例如標註 R5/R7「離線 holdout 已補充；production 對拍仍待 §0」；**不**因本計畫將 R4/R9 標為已排除 |

執行規範與 runbook 見 `investigations/test_vs_production/README.md`、`runbook.md`。

---

## 風險與驗收

| 風險 | 緩解 |
|------|------|
| OOM 或執行過慢 | 先 `--sample-rated` + 短 holdout；再擴大。 |
| Holdout 尾端 label 不完整 | 縮短 eval 迄日並在 notes 註明；與 `extended_end` 一致。 |
| 與線上數字誤比 | 每個數字旁註 **metric 名稱** 與 **資料來源 = Parquet**。 |

**最低驗收**：一張表含 **train_cutoff、holdout 範圍、固定閾值下 P/R、precision@recall=1%、（若已做）子窗變異**；並有一段文字說明本結果可支持或不可支持哪些 R 編號。

---

**文件版本**：初版，由離線調查討論整理寫入 `.cursor/plans/`。  
**最後更新**：2026-03-21
