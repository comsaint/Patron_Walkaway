# Phase 2 P0.2：Model Rollback Runbook

> 模型回滾程序：**僅允許整目錄替換**，禁止只更換單一檔案（例如 `model.pkl`）。  
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T3、trainer artifact 產出格式。

---

## 原則

1. **Rollback = 替換整個 artifact 目錄或 package**  
   必須以「一個完整、已驗證的 artifact 目錄」取代現有目錄，不得只複製或覆蓋部分檔案。

2. **禁止只換 `model.pkl`**  
   訓練產出的 artifact 為一組檔案，彼此一致（model、feature_list、feature_spec、reason_code_map、model_version、training_metrics 等）。只換 `model.pkl` 會導致：
   - 特徵順序或清單與 `feature_list.json` / `feature_spec.yaml` 不一致；
   - Scorer 載入時特徵對齊錯誤或推論結果錯誤。
   因此 **禁止** 僅替換 `model.pkl` 或任意單一檔作為 rollback。

3. **先查 provenance 再決定回滾版本**  
   回滾前應依 `doc/phase2_provenance_query_runbook.md` 以 `model_version` 在 MLflow 查詢該版本的 training window、git_commit 等，確認要回滾的版本無誤。

---

## Artifact 目錄結構（trainer 產出）

Scorer 讀取的目錄（預設為 `trainer/models/` 或 config 之 `MODEL_DIR`）應包含同一版本的完整 bundle，例如：

```
<MODEL_DIR>/
  model.pkl               # v10 單模型格式 {"model", "threshold", "features"}
  feature_list.json       # 特徵清單與 track
  reason_code_map.json    # SHAP reason code 對照
  model_version           # 版本字串，如 20260318-120000-abc1234
  training_metrics.json   # 訓練指標
  feature_spec.yaml       # 訓練時凍結的 feature spec（DEC-024）
  walkaway_model.pkl      # Legacy 單模型格式（scorer 相容）
```

以上檔案由 `trainer` 的 `save_artifact_bundle` 一次寫入，版本一致。

---

## Rollback 步驟（整目錄替換）

1. **決定目標版本**  
   自 MLflow 或既有備份取得要回滾的 `model_version`（例如 `20260315-100000-def5678`）。

2. **取得該版本的完整 artifact**  
   - 自備份還原：若你有依 `model_version` 備份的完整目錄，還原到一暫存路徑。  
   - 或自 MLflow artifact 下載：若 run 有上傳 artifact，自 MLflow 下載完整目錄。

3. **原子替換**  
   - 將目前 **scorer 使用的 MODEL_DIR** 重新命名或移開（例如 `models` → `models.old`）。  
   - 將目標版本的 **完整 artifact 目錄** 複製/移動為新的 `MODEL_DIR`（例如 `models`）。  
   - 確認新目錄內含 `model_version`、`model.pkl`、`feature_list.json`、`feature_spec.yaml` 等所有必要檔案。

4. **驗證**  
   - 讀取新目錄內 `model_version` 檔案，確認字串與預期一致。  
   - 啟動或重載 scorer，確認能正常載入並推論（可做一筆 smoke test）。

5. **後續**  
   - 舊目錄可於確認無誤後刪除或歸檔；若 rollback 有問題，可再將 `models.old` 換回。

---

## 注意事項

- **不要** 在 scorer 或服務運行中直接覆蓋單一檔案（例如只換 `model.pkl`）。應在停機或無流量時段做整目錄替換，或依部署流程做藍綠/金絲雀切換。
- **不要** 混用不同 `model_version` 的檔案（例如 A 版的 `model.pkl` 配 B 版的 `feature_list.json`）。
- Config 中 `MODEL_DIR`（或等同設定）須指向上述完整目錄；若部署用符號連結，應更新連結目標為新目錄而非改動目錄內單檔。

---

## 手動驗證建議

1. 依 runbook 用既有或測試用 artifact 目錄，模擬「重新命名舊目錄 → 複製新目錄為 MODEL_DIR」。
2. 確認 scorer 從新目錄載入後，`model_version` 與預期一致且推論正常。
3. 讓另一位維護者僅依本 runbook 操作一次，確認步驟可被跟隨且無歧義。
