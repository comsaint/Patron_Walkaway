# Model performance gap analysis
_Exported on 2026/4/30 at 18:42:51 [GMT+8] from Cursor (3.2.16)_

---

**User**

I have 2 models trained with same data source but different config and code version. I wonder what makes the gap between the model performance (the 'test_precision_prod_adjusted' which is our objective function, precision at alert level >=50 per hour). Can you please check what are the core reasons? Please feel free to spend more time on this complicated task.@.tmp/mlflow_export/ad525ee3d58d4ded8ad9d09bc98b46ed @.tmp/mlflow_export/b902d1fb9d8a431c8c0dbefafd1e1fc6 

---

**Cursor**

我會先把這兩個 `mlflow_export` run 的內容完整比對（參數、metrics、tag、可能的 artifact/設定差異），再把差異收斂成「最可能導致 `test_precision_prod_adjusted` 落差」的核心原因與證據鏈。先從讀取分析規格（canvas skill）開始，接著進行逐層比對。

我會先盤點這兩個 run 目錄的檔案結構，確認可用的比較素材（`run.json`、metrics、params、artifact），再做差異歸因。

我發現搜尋工具抓不到隱藏目錄，改成直接讀你指定路徑的 `run.json` 來做精準比對。

`.tmp` 被 `.cursorignore` 遮掉，讀檔工具無法直接存取；我會改用一個短的 Python 指令直接載入兩份 `run.json`，抽出 metrics/params/tags 差異來做根因分析。

我已拿到完整差異清單，但輸出非常大（含長欄位），接下來先抽取與目標函數最相關的關鍵欄位，再回推主因，避免被雜訊淹沒。

接著我會從目前程式碼中追 `test_precision_prod_adjusted` 的計算路徑，確認兩版是否可能「同名 metric 但定義不同」（這常是跨版本比較的關鍵陷阱）。

我已經鎖定主因，現在會做一個可視化 canvas，把「同一模型品質但因資料基準率差異導致 prod-adjusted 掉分」的證據與反事實分解放在同一頁，方便你直接檢視。

我已經完成兩個 run 的深入比對，並把完整證據整理成可視化 canvas：`canvases/mlflow-model-gap-root-cause.canvas.tsx`。  
你可以直接打開看重點表格與反事實分解。

核心結論先講：**差距主要不是模型排序能力變差，而是資料基準率（test neg/pos ratio）位移造成的 prod-adjusted 懲罰。**

- `test_precision_prod_adjusted`：A `0.5093` vs B `0.3746`（差 `0.1346`）
- 但 `test_precision`：A `0.6537` vs B `0.6752`（B 反而更高）
- `test_recall` 幾乎相同（`0.00930` vs `0.00923`）
- 最大差異在 `test_neg_pos_ratio`：A `3.6797` vs B `1.9285`
- 同時 production 假設比率固定：`6.6923`

這會直接拉低 prod-adjusted precision。  
我做了反事實分解（固定其他條件，只換 test neg/pos ratio）：

- B 若用 A 的 test ratio，`test_precision_prod_adjusted` 會從 `0.3746` 拉到 `0.5334`（+`0.1587`）
- A 若用 B 的 test ratio，會從 `0.5093` 掉到 `0.3523`（-`0.1570`）

`0.157` 的影響量級已經大於實際 gap `0.1346`，所以這是**第一主因**。

另外兩個重要次因：

- **程式版本確實不同**：`git_commit`（param）`d443245` vs `8d1c5d0`，MLflow tag commit 也不同。
- **資料組成明顯改變**：test positives 幾乎不變（約 `1.4M`），但 negatives 從 `5.14M` 掉到 `2.70M`，導致比例漂移。

我也發現一個比較風險：

- Run B 有完整 `alerts_per_hour >= 50` 相關 metrics（如 `test_alerts_per_hour = 51.97`），Run A 沒有這批欄位。這代表兩版「同名 metric 的計算脈絡」可能不完全一致（至少 logging/儀表層不同），比較時要保守解讀。

如果你要，我下一步可以直接幫你做「可行動」的 root-cause drill-down：  
1) 鎖定同一份 split snapshot 重跑兩版 code、2) 對每個 split 輸出 label 分佈與過濾前後 row lineage、3) 拆出是哪一段 filtering/樣本抽樣讓 negatives 大幅下降。

---

**User**

This is weird - the 2 models weere trained with same dataset, how can the positive/negative ratio changes so much?

---

**Cursor**

你這個質疑非常對，照理「同一資料窗」不該有這麼大比例變化；我先幫你把可能機制拆成「資料實際不同」vs「統計口徑不同」，並用程式碼路徑核對是哪一種。

你抓到重點了：**「同一資料來源」不等於「同一個實際進模型/評估的樣本集合」**。  
這次看起來就是後者變了。

先直接講結論：你兩個 run 的正負比大變，最可疑是 **負樣本下採樣 + 依列位置切分** 的組合，不是原始資料窗改了。

```3139:3156:trainer/training/trainer.py
# --- Per-chunk negative downsampling (OOM mitigation, config.NEG_SAMPLE_FRAC) ---
# Keep ALL positives; downsample negatives to neg_sample_frac fraction.
if neg_sample_frac < 1.0 and "label" in labeled.columns:
    _pos_mask = labeled["label"] == 1
    ...
    _neg_keep = labeled[~_pos_mask].sample(frac=neg_sample_frac, random_state=_chunk_seed)
    labeled = pd.concat([labeled[_pos_mask], _neg_keep], ignore_index=True)
```

而且 `NEG_SAMPLE_FRAC_AUTO` 預設是開的（會依 RAM 自動調整）：

```27:29:trainer/core/_config_training_memory.py
NEG_SAMPLE_FRAC_AUTO: bool = True
NEG_SAMPLE_FRAC_MIN: float = 0.05
NEG_SAMPLE_FRAC_ASSUMED_POS_RATE: float = 0.15
```

再來切分不是固定時間窗切，而是排序後按列比例切：

```9427:9434:trainer/training/trainer.py
_train_end_idx = int(n_rows * train_frac)
_valid_end_idx = int(n_rows * (train_frac + valid_frac))
_row_pos = np.arange(n_rows)
full_df["_split"] = np.select(
    [_row_pos < _train_end_idx, _row_pos < _valid_end_idx],
    ["train", "valid"],
    default="test",
)
```

所以只要前面下採樣比例變了，後面 train/valid/test 的邊界與組成都會變，`test_neg_pos_ratio` 就會大幅漂移。

你這兩個 run 的現象也符合這個機制：
- test positives 幾乎一樣（約 1.40M）
- test negatives 大幅下降（約 5.14M -> 2.70M）
- split 邊界時間也有偏移（約 1 小時內）

這不是「同一份最終樣本」。

---

如果你要快速驗證，我建議先查這三個證據（最省時間）：
1. 兩個 run 的 `training_metrics.json` 裡 `neg_sample_frac`（是否不同）。  
2. 訓練 log 裡 `[OOM-check]` 與 `Auto-adjusting NEG_SAMPLE_FRAC` 訊息。  
3. MLflow params 的 `split_*` 邊界（你已看到有差）。

如果你願意，我下一步可以幫你列一份「最小重現清單」，把 pipeline 鎖成真正可比（固定 `NEG_SAMPLE_FRAC_AUTO=False`, `NEG_SAMPLE_FRAC=1.0`, 固定 split parquet / seed / 特徵開關）。

---

**User**

Can you check if we log down the parameters related  to negative sampling e.g. NEG_SAMPLE_FRAC_AUTO and sent to MLFlow?

---

**Cursor**

正在搜尋 trainer 是否將 `NEG_SAMPLE_FRAC_AUTO` 等負樣本採樣相關參數寫入 MLflow。



結論先說：**`NEG_SAMPLE_FRAC_AUTO` 以及其它「設定檔層級」的負採樣開關，目前沒有透過 `log_params_safe` 寫進 MLflow run 的 params。**  
實際生效的 **`neg_sample_frac`（跑完後的有效值）** 主要是寫在 **artifact**（`training_metrics.json` / `model_metadata.json`），成功路徑下 **通常不會** 變成 MLflow 的 top-level param。

---

### 1. Provenance 階段寫入的 MLflow params（沒有負採樣）

`_log_training_provenance_to_mlflow` 組出來的 `params` 只有：`model_version`、`git_commit`、`training_window_*`、`artifact_dir`、`feature_spec_path`、`training_metrics_path`、diagnostics / metadata 路徑、可選的 `split_*`、以及 `mlflow_trained_model_artifact`。這裡**沒有** `NEG_SAMPLE_FRAC_AUTO` 或 `NEG_SAMPLE_FRAC`：

```8080:8107:trainer/training/trainer.py
    params = {
        "model_version": model_version,
        "git_commit": git_commit,
        "training_window_start": _start,
        "training_window_end": _end,
        "artifact_dir": artifact_dir,
        "feature_spec_path": feature_spec_path,
        "training_metrics_path": training_metrics_path,
        "pipeline_diagnostics_path": _pd_path,
        "pipeline_diagnostics_rel_path": _pd_rel,
    }
    if model_metadata_path:
        params["model_metadata_path"] = model_metadata_path
    if model_metadata_rel_path:
        params["model_metadata_rel_path"] = model_metadata_rel_path
    if split_boundary_params:
        params.update(split_boundary_params)
    _model_pkl = _artifact / "model.pkl"
    if _model_pkl.is_file():
        params["mlflow_trained_model_artifact"] = (
            f"{MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH}/model.pkl"
        )
    # T12: if pipeline already started a run (e.g. at pipeline entry), log to it; else start one.
    if has_active_run():
        log_params_safe(params)
    else:
        with safe_start_run(run_name=model_version):
            log_params_safe(params)
```

---

### 2. 訓練成功結尾寫入的 params（仍沒有負採樣）

成功時再 `log_params_safe` 的只有 OOM 預估相關與裝置相關欄位，**同樣沒有** `NEG_SAMPLE_FRAC_AUTO` / `NEG_SAMPLE_FRAC`：

```10327:10355:trainer/training/trainer.py
                oom_params = {
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                    "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
                }
                # Avoid logging None values (MLflow params do not accept nulls well).
                oom_params_clean = {k: v for k, v in oom_params.items() if v is not None}
                if oom_params_clean:
                    log_params_safe(oom_params_clean)

                log_params_safe(
                    {
                        "trainer_device_mode_requested": _REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS,
                        "trainer_device_mode_effective": (
                            "gpu"
                            if (
                                str(_EFFECTIVE_LIGHTGBM_DEVICE).lower() == "gpu"
                                or str(_LAST_GBM_BACKEND_EFFECTIVE_DEVICE).lower() == "gpu"
                            )
                            else "cpu"
                        ),
                        "gpu_fallback_used": str(
                            bool(_LIGHTGBM_GPU_FALLBACK_USED or _GBM_BACKEND_GPU_FALLBACK_USED)
                        ),
                        "lightgbm_device_requested": _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS,
                        "lightgbm_device_effective": _EFFECTIVE_LIGHTGBM_DEVICE,
                        "lightgbm_device_fallback": str(bool(_LIGHTGBM_GPU_FALLBACK_USED)),
                    }
                )
```

接著是把 `combined_metrics["rated"]` 等合併後 **`log_metrics_safe`**（只收可轉成 `float` 的 scalar），而 **`NEG_SAMPLE_FRAC_AUTO` 是 bool，本來就不會進這條 metrics 管線**；且從程式結構來看，`neg_sample_frac` 是寫在 **bundle 的 `training_metrics.json` 根層**，不是必然出現在 `rated` 那包要 log 的 metrics 裡。

---

### 3. 有效 `neg_sample_frac` 寫在哪裡（artifact，不是 MLflow param）

`save_artifact_bundle` 把 **`neg_sample_frac`**（此 run 實際使用的值）寫進 `training_metrics.json`：

```8278:8296:trainer/training/trainer.py
    _metrics_root: dict[str, Any] = {
        **combined_metrics,
        "model_version": model_version,
        # R301: record sampling metadata so artifacts can be audited
        # even when loaded later.  None = full rated population was used.
        "sample_rated_n": sample_rated_n,
        # R-NEG-2: record effective neg_sample_frac for auditability.
        # 1.0 = no downsampling; < 1.0 = negatives were downsampled.
        "neg_sample_frac": neg_sample_frac,
        # Production neg/pos ratio assumed for test_precision_prod_adjusted.
        # None = feature disabled (PRODUCTION_NEG_POS_RATIO not set in config).
        "production_neg_pos_ratio": PRODUCTION_NEG_POS_RATIO,
        # W2: operating contract for threshold objective / prod-adjusted semantics.
        "selection_mode": str(SELECTION_MODE or "field_test").strip() or "field_test",
```

`model_metadata` 裡也有 **`neg_sample_frac_effective`**（給文件契約用）：

```7928:7977:trainer/training/trainer.py
    sample_rated_n: Optional[int],
    skip_optuna: bool,
    neg_sample_frac_effective: float,
    bundle_dir: Path,
...
            "sample_rated_n": sample_rated_n,
            "neg_sample_frac_effective": float(neg_sample_frac_effective),
```

這些檔案會在成功時以 **`bundle/training_metrics.json`** 等形式 **當 artifact 上傳**（見 `log_artifact_safe` 迴圈），所以**可查**，但不是「MLflow Params 頁」上的 key。

---

### 4. 唯一會把 `neg_sample_frac` 當 **MLflow param** 的情況：訓練失敗

只有在 **pipeline 丟例外** 的 post-mortem 區塊，才會把 **`neg_sample_frac`: `_effective_neg_sample_frac`** 當 param 記一筆（仍是有效值，不是 `NEG_SAMPLE_FRAC_AUTO`）：

```10418:10431:trainer/training/trainer.py
                failure_params = {
                    "training_window_start": _iso_or_str(effective_start),
                    "training_window_end": _iso_or_str(effective_end),
                    "recent_chunks": recent_chunks,
                    "neg_sample_frac": _effective_neg_sample_frac,
                    "chunk_count": chunk_count,
                    "use_local_parquet": bool(use_local),
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                }
                # MLflow expects non-null scalar params; drop None.
                failure_params_clean = {k: v for k, v in failure_params.items() if v is not None}
                if failure_params_clean:
                    log_params_safe(failure_params_clean)
```

---

### 實務上你要怎麼查

- **MLflow UI → Params**：不要預期會看到 `NEG_SAMPLE_FRAC_AUTO`。  
- **MLflow UI → Artifacts → `bundle/training_metrics.json`**：看 **`neg_sample_frac`**（有效值）。  
- **`bundle/model_metadata.json`**：看 **`training_params.neg_sample_frac_effective`**（若 schema 有把 `training_params` 展開在那裡）。  
- **stdout / log**：`NEG_SAMPLE_FRAC_AUTO` 相關行為多半只在 **`[OOM-check]` / `OOM probe`** 的 **logger / print** 裡。

若你希望「之後每個 run 都能在 MLflow Params 直接比對 `NEG_SAMPLE_FRAC_AUTO` / config 的 `NEG_SAMPLE_FRAC`」，需要改程式把這些欄位加進 `log_params_safe`（你目前是 Ask mode，我無法直接改；若要我做可以切到 Agent mode）。

---

**User**

Can you check what are the differences between the data set they used and how it was split?

---

**Cursor**

從先前匯出的 MLflow run 與程式碼邏輯，整理兩次 run 的「資料窗／切分」差異；若 artifact 可讀會再補上 `model_metadata.json` 的細節。



`.tmp` 匯出目錄在這邊無法直接讀檔，以下依你先前兩個 run 的 **`run.json` params／metrics**（我們已解析過的內容）加上 **`trainer.py` 的切分實作** 說明差異。

---

## 1. 「用的資料」哪裡相同、哪裡不同

**相同（可視為同一資料契約／來源設定）**

- **訓練時間窗**：`training_window_start` / `training_window_end` 都是 `2026-01-01`～`2026-04-02`（params 一致）。
- **特徵規格**：`feature_spec_path` 與 **`feature_spec_sha256` 相同**（兩 run 的 `features_candidates.yaml` 指紋一致）。
- **Test 區間上界**：`split_test_end` 都是 `2026-04-01T23:59:59`。

**不同（實際進模型的「樣本集合」已變）**

- **列數（materialized rows）**差很多，例如（來自當時的 metrics）：
  - `train_samples`：約 **30,516,376** vs **19,125,563**
  - `val_samples` / `test_samples`：約 **6,539,224** / **6,539,224** vs **4,098,335** / **4,098,336**
- **標籤分佈**：`test_positives` 幾乎一樣（約 1.40M），但 **負例數大幅下降**，所以 `test_neg_pos_ratio` 從約 **3.68** 變 **1.93**。

這代表：**不是「換了另一份 raw 表」那麼單純，而是同一條 pipeline 下，中間產生的列集合（尤其負例）不一樣**——常見原因包含 **負例下採樣 `neg_sample_frac`、快取／重算、或 Step 6/7 路徑差異** 等；這些不會改 `feature_spec_sha256`，但會改實際 row 數。

---

## 2. 「怎麼切分」——規則相同，邊界時間不同

### 規則（兩次 run 共用同一套程式邏輯）

Step 7 會依 **`payout_complete_dtm`** 排序（再依 `canonical_id` / `bet_id` 若有則做 stable tie-break），然後用 **列位置比例** 切 train / valid / test（`TRAIN_SPLIT_FRAC`、`VALID_SPLIT_FRAC`），不是用「固定日曆 cutoff」一條 SQL 切：

```9427:9438:trainer/training/trainer.py
                _train_end_idx = int(n_rows * train_frac)
                _valid_end_idx = int(n_rows * (train_frac + valid_frac))
                _row_pos = np.arange(n_rows)
                full_df["_split"] = np.select(
                    [_row_pos < _train_end_idx, _row_pos < _valid_end_idx],
                    ["train", "valid"],
                    default="test",
                )
                _split_col = full_df["_split"]
                train_df = full_df[_split_col == "train"].reset_index(drop=True)
                valid_df = full_df[_split_col == "valid"].reset_index(drop=True)
                test_df = full_df[~(_split_col.isin(("train", "valid")))].reset_index(drop=True)
```

因此：**總列數 `n_rows` 或排序後的列順序一變，同一個 `train_frac` 也會對應到不同的時間邊界**（你 MLflow 上的 `split_*` 就是各 split 內 `payout_complete_dtm` 的 min/max 摘要）。

### 邊界（你兩個 run 的 `run.json` params）

| 邊界 param | Run A (`ad525…`) | Run B (`b902…`) |
|-----------|------------------|-----------------|
| `split_train_end` | `2026-03-02T14:24:09` | `2026-03-02T13:09:51` |
| `split_valid_start` | 同 train_end | 同 train_end |
| `split_valid_end` | `2026-03-17T16:22:37` | `2026-03-17T15:56:37` |
| `split_test_start` | `2026-03-17T16:22:37` | `2026-03-17T15:56:37` |

也就是：**valid/test 的起點大約差 26 分鐘；train 結束大約差 1 小時 14 分**。在「依排序後列位置切」的前提下，這與 **總列數變少／排序後序列變了** 是一致的。

### MLflow 上的 split 參數是「摘要」不是「切分規則本身」

`split_row_metadata_to_mlflow_string_params` 只把各 split 的 **`start`/`end`（時間字串）** 寫成 MLflow string params，**不會**寫 `TRAIN_SPLIT_FRAC`、`NEG_SAMPLE_FRAC` 等：

```7886:7900:trainer/training/trainer.py
def split_row_metadata_to_mlflow_string_params(
    splits: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Flatten split ``start``/``end`` into MLflow string params (length-capped)."""
    _max = 200
    out: dict[str, str] = {}
    for split_name in ("train", "valid", "test"):
        block = splits.get(split_name) or {}
        for k in ("start", "end"):
            v = block.get(k)
            if v is None:
                continue
            key = f"split_{split_name}_{k}"
            s = str(v)
            out[key] = s if len(s) <= _max else s[:_max]
    return out
```

若要精確比對「比例與 rated-only 摘要」，要看 bundle 裡的 **`model_metadata.json`**（含 `splits` / `model_used_splits` 等）；你本機若仍有該目錄，可直接打開兩個 `model_version` 目錄對照。

---

## 3. 一句話總結

- **資料來源設定**（時間窗、feature spec）對齊；**實際矩陣列數與正負比**不一致。  
- **切分演算法**相同（排序 + 列比例）；**切出來的時間邊界與各 split 列數**因 **排序後總列數／內容** 變了而跟著變。

若你希望我把「`model_metadata.json` 裡 `splits` vs `model_used_splits`」逐欄對照成表，需要能讀到兩個 bundle（或你把兩份 `model_metadata.json` 貼到 workspace 非 `.tmp` 路徑）；目前是 Ask mode 且 `.tmp` 讀取被拒，無法在這邊自動開檔補齊那一層。

---

**User**

Can I understand that "train_samples：約 30,516,376 vs 19,125,563" may be due to difference in NEG_SAMPLE_FRAC, which does not impact the val_samples / test_samples?

---

**Cursor**

不能這樣理解。**在目前的 pipeline 裡，`NEG_SAMPLE_FRAC`（實際是 `neg_sample_frac`）會作用在 Step 6 的每個 chunk 上，發生在 Step 7 做 train/valid/test 切分之前**，所以**會一併影響 train、valid、test 三個 split 的列數**（主要是負例變少、正例全留）。

相關邏輯是：先對 labeled 資料做負例下採樣，再寫成 chunk parquet；之後 Step 7 才 concat、排序、依列比例切分。

```3139:3156:trainer/training/trainer.py
    # --- Per-chunk negative downsampling (OOM mitigation, config.NEG_SAMPLE_FRAC) ---
    # Keep ALL positives; downsample negatives to neg_sample_frac fraction.
    if neg_sample_frac < 1.0 and "label" in labeled.columns:
        _pos_mask = labeled["label"] == 1
        ...
        _neg_keep = labeled[~_pos_mask].sample(frac=neg_sample_frac, random_state=_chunk_seed)
        labeled = pd.concat([labeled[_pos_mask], _neg_keep], ignore_index=True)
```

接著才是排序後依比例切 train/valid/test：

```9427:9438:trainer/training/trainer.py
                _train_end_idx = int(n_rows * train_frac)
                _valid_end_idx = int(n_rows * (train_frac + valid_frac))
                ...
                train_df = full_df[_split_col == "train"].reset_index(drop=True)
                valid_df = full_df[_split_col == "valid"].reset_index(drop=True)
                test_df = full_df[~(_split_col.isin(("train", "valid")))].reset_index(drop=True)
```

因此：

- **`train_samples` 差很多** *可以* 與 **`neg_sample_frac < 1`** 有關（連同 valid/test 一起變）。
- **「不影響 `val_samples` / `test_samples`」在這份程式裡不成立**；若只有 train 變、val/test 完全不變，反而比較不像單純全域 `neg_sample_frac` 的效應，要懷疑別的路徑（例如不同 run 的資料量定義、是否只載入部分 split、或 metrics 來自不同階段等）。

你手上的數字其實是三邊**一起**縮小（約同一個量級）：train 約 0.63 倍、val/test 也約 0.63 倍，這和「切分前先整體少了很多負例」是相容的；若要**坐實**是 `neg_sample_frac`，仍應以 artifact 裡的 **`training_metrics.json` 的 `neg_sample_frac`**（或 `model_metadata` 的 `neg_sample_frac_effective`）對兩個 run 核對。

---

**User**

Then I cannot think of a reason why the train_samples differ dramatically, while val_samples and test_samples remains roughly the identical given the same data source (proven by fingerpint and roughly same date boundary). Any thoughts?

---

**Cursor**

你這段敘述裡，有一點需要先校正：**在你這兩個 run 的數字裡，`val_samples` / `test_samples` 並沒有「維持差不多、只有 train 劇變」**；三個 split **是同一個比例一起縮下去的**。

用你之前的數字算比例（B / A）：

| split | Run A | Run B | B/A |
|--------|--------|--------|-----|
| train | 30,516,376 | 19,125,563 | **≈ 0.627** |
| val | 6,539,224 | 4,098,335 | **≈ 0.627** |
| test | 6,539,224 | 4,098,336 | **≈ 0.627** |

三個都是 **~37% 列數消失**，這種「**總列數先少一截，再照固定比例切 train/val/test**」的形狀，和 pipeline 設計是一致的：Step 6 若在 chunk 上做了負例下採樣（或其它會減列的步驟），**進 Step 7 的 `n_rows` 變小**，後面 `70% / 15% / 15%`（依你專案設定的 `TRAIN_SPLIT_FRAC`、`VALID_SPLIT_FRAC`）切下去，**三塊會同比例變小**。

另外你可能混淆了兩種「identical」：

1. **同一 run 裡 `val_samples ≈ test_samples`**  
   這很正常：切分是依排序後的列位置，train 佔前段、valid 中段、test 尾段；若 **valid 與 test 佔的列比例接近**（例如都是剩餘 window 的一半一半），兩個數字會很接近（你甚至看到 A 的 val/test 完全一樣）。

2. **兩個 run 之間 val/test「沒變」**  
   在你這組數據裡**不成立**；val/test 也有明顯下降，只是**絕對差（幾百萬列）看起來沒 train 那麼聳動**，但**相對降幅和 train 一樣**。

---

若要在「指紋與日期邊界差不多」的前提下解釋「為何整體列數少了一截」，合理方向包括（不必互斥）：

- **`neg_sample_frac`（含 `NEG_SAMPLE_FRAC_AUTO` 自動調降）**：理論上會讓 **train/val/test 同時** 依負例比例縮小（正例全留），比例常會很接近你看到的 ~0.63。  
- **快取／強制重算（`force_recompute`）、chunk 集合、`--recent-chunks`、或某次 run 少處理了部分 chunk**：也會讓 **進 Step 7 的總列數** 變少，三 split 同樣跟著縮。  
- **「資料來源指紋」只證明 feature spec 相同**，不保證 **ETL 產物列數、neg sample、identity 模式** 等中間產物相同。

若要一句話收斂：**你觀察到的現象更像是「總體可切分列數變少」，而不是「只有 train 被動了手腳」**；而且從數字上看，**val/test 其實也有同等幅度的變化**，只是容易被「train 的絕對差很大」帶過去。

---

**User**

Oh I see... so likely there was a different sampling ratio applied indeed. Then a followup question: let's assume it was really just due to a difference in the negative sampling ratio. In our code, ffter training models, their precision were about the same level at 0.6x. In order to estimate their precision in production, we adjust the precision with the neg:pos ratio. Is this a correct approcah to estimate the true precision?

---

**Cursor**

很好的追問。我先直接對你問題給結論，再用你們目前實作公式與前提假設拆解「什麼時候對、什麼時候會失真」。

你的方向是**對的，但有條件**。  
短答：**這是合理的一階校正（first-order correction），不是保證準確的「真實 precision」。**

你們現在的實作是：

```5122:5161:trainer/training/trainer.py
def _precision_prod_adjusted(prec, *, production_neg_pos_ratio, test_neg_pos_ratio):
    ...
    scaling = pn / tn
    term = (1/p - 1) * scaling
    adj = 1 / (1 + term)
```

這個公式本質上是在假設：

1. 門檻固定下，模型的 **TPR/FPR 不變**（只換 base rate）  
2. train/test 跟 production 的差異主要是 **label prior shift（正負比例變）**  
3. 你的負樣本抽樣是「隨機且不偏」的（不改變條件分佈）

在這些假設成立時，這種 `prod_adjusted_precision` 是**正確且常用**的。

---

### 什麼時候會失真（你要小心的點）

- **不是只有 prior shift**：如果 production 還有 covariate/concept drift，TPR/FPR 會變，公式就會偏。
- **負樣本抽樣不是純隨機**：若抽樣機制跟特徵或難例有關，test precision 不再能代表原始分佈。
- **`production_neg_pos_ratio` 估錯**：這個值很敏感，估錯會直接把 adjusted precision 拉歪。
- **門檻策略改變**：若上線不是同一 threshold 或有動態流量控制，離線校正就不等價。

---

### 我對你問題的結論

- **作為離線估算：是正確且值得保留的做法。**
- **作為「true production precision」：不該當真值，只能當估計值。**

你現在最穩健的做法是：  
把它當 **expected precision under prior shift**，並用線上延遲標籤回填做持續校正（例如每週重估 `production_neg_pos_ratio` 與實際 precision gap）。  

如果你要，我可以下一步幫你設計一個「離線 adjusted vs 線上實測 precision」的監控框架（最少欄位版）。

---

**User**

Back to the two models. I am worry about why such a big gap: "最大差異在 test_neg_pos_ratio：A 3.6797 vs B 1.9285". Any reason?

---

**Cursor**

你這個擔心非常合理，而且這個 gap 的確偏大。  
先講我的判斷：**最可能是「負樣本被額外下採樣」**，而且很可能是自動機制觸發，不是你手動改 config。

### 為什麼我這樣判斷（證據形狀）
從你兩個 run 的數字看：

- `test_positives` 幾乎不變（約 `1.40M`）
- `test_negatives` 明顯下降（約 `5.14M -> 2.70M`，只剩約 `52.5%`）
- 所以 `test_neg_pos_ratio` 從 `3.6797` 掉到 `1.9285`

這種「**正例幾乎不動、負例大減**」的形狀，最像 `neg_sample_frac < 1` 的效果。

---

### 程式上最可疑的機制（高機率）
你們 trainer 是先做負例下採樣，再切 train/valid/test：

- 負採樣（保留全部正例、抽樣負例）在 `process_chunk`  
- 而且有 `NEG_SAMPLE_FRAC_AUTO` 的 OOM 自動調整，會依當下 RAM/chunk size 動態降比例  
- 再來 Step 7 才按列位置切 split

所以即使資料窗和 feature spec 一樣，只要 auto 決策不同，`test_neg_pos_ratio` 就會變很多。

---

### 還有什麼次要可能（中機率）
如果最後確認 `neg_sample_frac` 其實沒變，那就要看：

1. **上游過濾規則改了**（不同 commit：`d443245` vs `8d1c5d0`）  
   例如 label/rated/filtering 邏輯若偏向移除負例，也會造成同樣形狀。
2. **chunk 重算/快取路徑差異**  
   有可能部分 chunk 走重算、部分走舊 cache，導致有效 row 組合不同。
3. **split 是按列比例切，不是固定時間點切**  
   row 組合一變，`split_*` 邊界時間自然會漂移。

---

### 最快驗證（建議你先做）
1. 比兩個 run 的 artifact `training_metrics.json` 裡的 `neg_sample_frac`  
2. 看訓練 log 是否出現 `OOM-check` 與 `Auto-adjusting NEG_SAMPLE_FRAC`  
3. 比 `model_metadata.json` 的 `splits`（rows/positives/negatives）

如果你要，我可以下一步幫你做一個「5 分鐘就能定位」的檢查清單（照順序跑，跑到第幾步就能定案）。

---

**User**

I will check that once I have access to that log. But now let's assume it was really due to neg_sample_frac and nothing else. Can I conclude that if we use more samples (i.e. neg_sample_frac approaches 1), in general the model precision improves?

---

**Cursor**

不能直接這樣下結論。  
**`neg_sample_frac` 越接近 1，不代表 precision 一定提升**；它主要是改變「你拿多少負例來訓練」，效果通常是 trade-off。

## 直觀結論（先給你可用版）

- `neg_sample_frac ↑`（負例更多）常見會讓模型 **更保守**：  
  - **FP 下降**（好處）  
  - **TP 也可能下降**（壞處）  
- 在固定 alert-volume / recall 約束下，最終 precision 可能上升、持平、或下降，**沒有單調保證**。

## 為什麼沒有單調性

即使其他都不變，增加負例會同時影響：
1. 決策邊界（模型看到更多 hard negatives，排序會重排）
2. 機率校準（score 分布形狀變）
3. 你後續 threshold 選擇（你們是有目標約束的選點，不是固定 threshold）

所以「更多資料」在統計上通常有助穩定與泛化，但對單一指標（尤其 constrained precision）不一定單調。

## 你這個專案特別要注意的點

你們還有 `test_precision_prod_adjusted`，它受 `test_neg_pos_ratio` 影響很大。  
當 `neg_sample_frac` 變小，`test_neg_pos_ratio` 變小，prod-adjusted 會被拉低；這個變化有時候比模型本體變化還大。  
所以看到 precision 變動時，要分清是：
- **模型真的更好/更差**，還是
- **抽樣造成 base-rate 校正項在動**。

## 在你的假設下（只變 neg_sample_frac）比較好的說法

你可以這樣說，而不是「precision 一定變好」：

- `neg_sample_frac` 越高，通常 **offline 估計更接近未下採樣母體**，variance 也較低；
- 但在固定業務約束下，precision **需以實驗曲線驗證**，不能憑理論單點判斷。

如果你要，我可以幫你定一個最小實驗矩陣（例如 `0.2/0.4/0.6/0.8/1.0`）跟該看的 4 個核心指標，讓你一次看清最佳區間。

---

**User**

But our objective is 'precision at a certain alert level' so it is indeed a 單點判斷. The reason why we calculate the test_precision_prod_adjusted is because we thought this mimics the true precision in production (assume no drift etc.). In our 2 models, we saw that the model with higher neg_sample_frac reaches a better test_precision_prod_adjusted. If the way we do adjustment is correct, wouldn't it prove that higher neg_sample_frac leads to better test_precision_prod_adjusted?

---

**Cursor**

你這個問題問得非常精準。  
答案是：**不會「證明」一般性結論**，但會是**強證據**。關鍵在這一點：

## 如果 adjustment 完全正確、且只有 base rate 變
你們的公式是

\[
P_{adj}=\frac{1}{1+\left(\frac{1}{P_{test}}-1\right)\frac{r_{prod}}{r_{test}}}
\]

其中 \(r=\frac{neg}{pos}\)。

在理想條件下（無 drift、同一模型同一 operating point），這個調整其實是在把 test 的 prevalence 轉回 production prevalence。  
**理論上應該讓結果對 \(r_{test}\) 近似不敏感**。  
也就是說：若只是「抽樣比例不同」，`test_precision_prod_adjusted` 應該接近，不該系統性變高/變低。

---

## 那你看到「高 neg_sample_frac 的模型更好」代表什麼？
更合理的解讀是：

1. **模型本身在該 alert level 真的變好了**（例如 FPR 降了），或  
2. 門檻選擇/訓練流程造成 operating point 改變，或  
3. 有有限樣本噪音。  

但這仍是**兩點觀察**，不是「對所有情況都成立的單調定律」。

---

## 你原問題的精準回答
- 你的 objective 雖然是單點（某 alert level），沒錯。  
- 但從「兩個模型」看到一次提升，**不能推出**「`neg_sample_frac` 越高，`test_precision_prod_adjusted` 必然越高」。  
- 最多能說：**在這次條件下，高 `neg_sample_frac` 與較高 adjusted precision 同時出現，疑似有正向效果**。

如果你要把它升級成可下決策的結論，最小需要固定其他條件做 `neg_sample_frac` sweep（例如 0.3/0.5/0.7/1.0）看同一 objective 是否呈現穩定單調。

---

**User**

OK get it, so in principle it seems true but need more evidences. Now about the way we do sampling. The reason we did that was due to OOM issue and computation time. I begin to wonder if we are using the correct way to sample. Now we first apply negative sampling to the whole dataset and then split it to train/val/test. But perhaps we should only apply negative sampling to the training set? What do you suggest?

---

**Cursor**

你這個直覺是對的，而且我會直接講：**以你們的目標（`precision at alert level`）來說，現在「先全體負採樣再切 split」不是最佳做法。**  
我建議改成：**先切 train/val/test，再只對 train 做負採樣**。

## 為什麼（重點）

- 你們的 objective 是偏營運指標（precision + alert volume），`val/test` 應盡量代表真實分佈。  
- 目前做法會把 `val/test` 的 base rate 也改掉，導致你後面需要 `prod_adjusted` 去補，風險和方差都變大。  
- 尤其你們還有 `alerts_per_hour >= 50` 這類約束，`val/test` 被抽樣後，這類量會被扭曲（即使你再調整 precision，alert volume 還是會受影響）。

---

## 建議方案（務實版）

1. **Split first（不抽樣）**：先拿完整資料切出 train/val/test。  
2. **Train-only sampling**：只在 `train` 對 negatives 做 sampling（你們為了 OOM/time 的原始目的）。  
3. **Val/Test full-distribution**：`val/test` 保持完整（至少 rated 子集完整）。  
4. **Threshold selection 用完整 val**：這樣選出的 threshold 才對上線場景更穩。  
5. **Test 報告用完整 test**：可同時報 raw precision 與 prod-adjusted，但主報告建議看 raw-on-full-test + 業務約束達成率。

---

## 如果 val/test 太大，真的撐不住怎麼辦？

可退而求其次：

- val/test 不得不抽樣時，**固定抽樣規則**（分層、固定 seed、固定比例）；
- 用 **重要性權重**（inverse sampling probability）回推評估；
- 另外保留一個較小但**不抽樣**的「golden holdout」專門做 threshold sanity check。  

但這仍次於「val/test 不抽樣」。

---

所以結論很明確：  
**你要解決 OOM/time，負採樣應該主要發生在 train；不要污染 val/test。**  
你現在想改這點，是正確方向。

---

**User**

I agree that this is the way to go. About the "val/test 太大" issue: can you check my pipeline to see if there is any chance to avoid while not performing sampling? Say batch processing etc.

---

**Cursor**

我會先快速掃你現有 pipeline 的「省記憶體路徑」：哪些步驟已經支援 on-disk / batch，以及哪裡還在把 val/test 全量載入記憶體，然後給你一個不靠抽樣的落地方案。

可以，而且你目前的 pipeline **已經有不少「不抽樣也省記憶體」的路徑**。  
我看完程式後，結論是：**主要瓶頸不在 Step 7，而在 Step 9 某些分支仍會把 rated val/test 整包載入記憶體。**

## 你已經有的省記憶體設計（可保留 full val/test）
1. **Step 7 train 不落記憶體**
   - `STEP7_KEEP_TRAIN_ON_DISK=True` + `STEP9_EXPORT_LIBSVM=True` 時，train/valid/test split 可保留在磁碟。
2. **LibSVM 匯出是 batch 串流**
   - `_export_parquet_to_libsvm()` 用 `fetchmany(batch_size=50000)` 逐批輸出，不會一次吃整包。
3. **test 可直接從 LibSVM 檔推論**
   - 在 B+ 路徑，test 可走 `booster.predict(str(test_libsvm_path))` + `_compute_test_metrics_from_scores()`，不需先建 `test_df`。

---

## 目前會把 val/test 拉進記憶體的關鍵點（這才是你要避開的）
1. **Step 9 一開始會載入 rated validation parquet**
   - `valid_df.empty` 且有 `valid_split_parquet_path` 時，會呼叫 `_load_rated_eval_split_from_parquet(...)`（`pd.read_parquet`）。
2. **A3 GBM bakeoff 會載入 rated valid/test**
   - `gbm_bakeoff=True` 時，若 memory 中沒有，會從 parquet 把 valid/test 載進來比模型。
3. **A4 two-stage 也可能載入 valid/test**
   - A4 路徑有多處 fallback，會從 parquet 讀 rated valid/test 進來做 stage1/2 與融合評估。

---

## 不抽樣前提下，我建議你先這樣跑（最務實）
如果你目標是「full val/test，不做 sampling，同時避免 OOM」：

- 保持：
  - `STEP7_USE_DUCKDB=True`
  - `STEP7_KEEP_TRAIN_ON_DISK=True`
  - `STEP9_EXPORT_LIBSVM=True`
  - `STEP9_TRAIN_FROM_FILE=True`
- 關閉高記憶體分支：
  - `--no-gbm-bakeoff`
  - `A4_TWO_STAGE_ENABLE_TRAINING=0`（必要時 inference 也先關）
- 視需要：
  - `--skip-optuna`（減少 validation 分支的額外壓力）

這組合通常可大幅降低 val/test 被整包讀進 RAM 的機會，而且**不需要抽樣 val/test**。

---

## 你問的「batch processing 能不能再做」
可以，但你現在程式裡的 batch 主要在：
- LibSVM 匯出（已有）
- 內容內部 predict（有 batch helper）

真正還可優化的是：把「從 parquet 載入 rated valid/test DataFrame」改成**逐批讀檔 + 逐批推論 + 線上累積指標**。  
這是可行，但需要改程式（你現在 Ask mode，我先不動 code）。

---

如果你要，我可以下一步直接給你一份「**最低風險設定檔**」：哪些開關必開/必關，來確保 `val/test` 不抽樣且最大化避免 OOM。

---

**User**

But now didn't we already sample the whole dataset before splitting? How about the steps before step 7 e.g. step 6 which is the most time consuming part?

---

**Cursor**

你問到核心痛點了，答案是：**對，現在確實是「Step 6 先做全資料處理，再在 chunk 內做負採樣，然後才到 Step 7 切 split」**。  
所以你擔心是正確的。

```3139:3156:trainer/training/trainer.py
# --- Per-chunk negative downsampling ---
# Keep ALL positives; downsample negatives to neg_sample_frac fraction.
if neg_sample_frac < 1.0 and "label" in labeled.columns:
    ...
    _neg_keep = labeled[~_pos_mask].sample(frac=neg_sample_frac, random_state=_chunk_seed)
    labeled = pd.concat([labeled[_pos_mask], _neg_keep], ignore_index=True)
```

而 split 是在 Step 7 才做（排序後按列位置切）：

```9427:9438:trainer/training/trainer.py
_train_end_idx = int(n_rows * train_frac)
_valid_end_idx = int(n_rows * (train_frac + valid_frac))
...
full_df["_split"] = np.select(... ["train", "valid"], default="test")
```

---

### 這代表什麼？

你說的沒錯：  
- **Step 6（最耗時）很多工作已經做完才抽樣**，所以抽樣對 Step 6 的 CPU/ETL 成本幫助有限（但對後續 I/O、Step7~9 記憶體與時間有幫助）。  
- 若 `neg_sample_frac=1`，Step 6 產出更大，後面會更重。

---

### 為什麼「只抽 train」現在不容易直接做？

因為你們目前 split 定義是「**全量排序後依 row index 比例切**」，這個 split 身分（train/val/test）在 Step 7 前其實還不知道。  
=> 在 Step 6 當下，無法精準知道某筆最終會落 train 還是 val/test。

---

### 你現在的兩個選項

1. **維持現有 row-level split 邏輯**  
   - 很難在 Step 6 就做「train-only sampling」。
   - 要做到只能先算出全域切分邊界（額外 pass）再回頭處理。

2. **改成時間邊界切分（固定 cutoff）**  
   - 你可以在 Step 6 就知道是否 train/val/test。  
   - 就能只對 train 負採樣，val/test 保完整。  
   - 但這是切分定義變更，需評估對歷史可比性。

---

所以結論是：  
**你現在的架構確實讓 Step 6 先吃大成本，再抽樣；而且這個 split 設計本身就是 train-only sampling 的主要阻礙。**  
你如果要，我可以下一步幫你設計「不改業務語義、最小改動」的兩階段方案（先求全域邊界，再做 train-only sampling）。

---

**User**

I think the pain point is not about spliting etc., but the fact that we often need to recompute the whole dataset (mostly t_bet, which is huge) in the pipeline for example in step 6. Though we have implemented a lot of measures e.g. OOM prevention, caching etc., still we often find ourselves (re-)computing the same thing on a same dataset again and again across training jobs. I am seeking a long-term solution in which (assume past data no longer change) we organize the data in chunks e.g. by gaming day, do 1 round of (heavy) computation, and reuse the result in future runs. Meanwhile, I want to keep it flexible enough to leave room for possible new features in future. Any idea how this can be achieved?

---

**Cursor**

這個方向非常對，我先對照你現有的 chunk cache 設計，給你一個「可長期演進、又能因應新特徵」的分層資料資產方案（含 migration 路徑，不推翻重來）。

你這個方向非常好，而且你現在的 code 其實已經有雛形（chunk cache + sidecar + fingerprint）。  
問題是目前 cache key 綁太多「訓練期參數」，導致很容易失效、重算整批（你看到的 pain）。

我建議做成「**分層資料資產 + 增量物化**」：

## 核心設計（長期解）
### 1) 拆成三層，不同失效邏輯
- **L0 Raw（日分區，不可變）**  
  `t_bet`、session 原始快照，按 `gaming_day` 存。
- **L1 Heavy 基礎特徵層（最貴，重用最多）**  
  把 Step 6 最重的 join/clean/label 前處理做成「日分區 precompute parquet」。
- **L2 Training-ready 層（輕組裝）**  
  根據 run 的 window/split 規則，把 L1 分區拼起來；sampling、權重、export（LibSVM）放這層。

關鍵：**neg_sample_frac 不該參與 L1 key**，只影響 L2。  
這樣改完，調整 sampling 不會重算 `t_bet` 重工。

---

### 2) 用「Manifest + Registry」管理分區
每個 `gaming_day` 產一筆 registry（SQLite/DuckDB 都可）：
- source_hash（raw snapshot 指紋）
- transform_version（程式版本）
- feature_family_version（特徵族版本）
- partition_status（ready/failed/stale）
- output_path + row_count + min/max ts

每次訓練先做 manifest：
- 需要哪些 day
- 哪些 day 已 ready -> 直接讀
- 哪些缺/過期 -> 只補算那些 day

---

### 3) 特徵彈性：分「穩定核」與「實驗層」
你擔心未來新特徵，這點可以靠分層解：
- **stable_core_features**：高重用、昂貴，進 L1
- **experimental_features**：變動快，放 L2 即時計算或小成本物化

避免每次改一個實驗特徵就炸掉整個歷史 cache。

---

## 為什麼你現在會一直重算
你在 `trainer/training/trainer.py` 的 chunk cache 已有 fingerprint，但它目前會受 `feature_spec_hash`、`neg_sample_frac` 等影響。  
這代表「訓練策略調整」會連帶使 Step 6 cache miss，導致重跑重工。  
（你已有 prefeatures cache 的概念，這其實就是正確方向，只是還沒把層次切乾淨。）

---

## 你可以先做的最小改造（不推翻）
1. 先把 Step 6 拆成 `build_prefeatures(day)` 與 `assemble_training(day-range)` 兩段。
2. 讓 `neg_sample_frac` 只進 `assemble_training`。
3. 把 `feature_spec_hash` 拆成：
   - `core_feature_hash`（L1）
   - `exp_feature_hash`（L2）
4. 先從 `t_bet` 最重那段開始日分區物化，其他慢慢搬。

---

如果你要，我可以下一步幫你出一版「**Implementation plan（只講 how，不混 SSOT/工作拆單）**」：包含資料表 schema、key 設計、失效規則、回填策略、以及如何從現有 `process_chunk` 平滑遷移。

---

**User**

I am also thinking about grouping the bets to different layers. Logically, we now have a definition of 'run', which is 'consecutive bets with no more than 30 minutes in between'. If we have a run-level table, we can craft run-level features while avoiding loading all the bets in that run. Then on top of 'run' we define a 'trip' level, which is defined as no bet in consecutive 3 gaming days, which enable us to craft trip-level features. If the definition and data do not change, I want a design that helps to craft richer features, reduce re-computation, yet flexible enough to accomodate feature changes. Any thoughts?

---

**Cursor**

這個方向非常好，而且我會說是**長期正解**。  
你的 `bet -> run -> trip` 分層，本質上就是把最重的事件序列計算做成可重用資產。

## 我對你提案的評估（先講結論）
- **可行且值得做**：能同時提升 feature richness、減少重算。
- **要小心兩件事**：  
  1) 邊界狀態（跨日/跨批次）  
  2) 版本治理（定義變更時不能污染舊結果）

---

## 建議的資料模型（最小可落地）
### 1) 實體層（不可變輸入）
- `bet_event`（原始下注事件，含 event_time, gaming_day, player_id, bet_id）

### 2) 聚合層（可重用核心）
- `run_fact`
  - 定義：同一玩家，相鄰 bet 間隔 <= 30 分鐘
  - 欄位：`run_id`, `player_id`, `run_start_ts`, `run_end_ts`, `bet_count`, `sum_amt`, ...
- `trip_fact`
  - 定義：同一玩家，若連續 3 個 gaming day 無 bet 則切新 trip
  - 欄位：`trip_id`, `player_id`, `trip_start_day`, `trip_end_day`, `run_count`, `bet_count`, ...

### 3) Feature 層（可快速演進）
- `feature_run_v*`（run-level features）
- `feature_trip_v*`（trip-level features）
- `feature_bet_snapshot_v*`（最終訓練快照，as-of join run/trip features）

---

## 你要的「彈性 + 少重算」關鍵設計
- **定義版本化**（超重要）
  - `run_definition_version`（例如 `gap_30m_v1`）
  - `trip_definition_version`（例如 `idle_3_gday_v1`）
- **feature 版本化**
  - `feature_family` + `feature_version`
- **物化分區**
  - 以 `gaming_day`（或 month）分區，增量更新
- **As-of join 合約**
  - 明確 PIT：bet 只能看到 bet 當下以前已知的 run/trip 資訊

這樣即使你未來改特徵，也只重算對應 feature layer，不用重建整個 run/trip。

---

## 兩個最常踩雷（先幫你避開）
1. **Run/Trip 不是純日內可獨立計算**  
   - Run 30 分鐘會跨日；Trip 3 gaming days 一定跨日。  
   - 需要「前一分區尾巴狀態」(carry-over state) 才能增量正確。
2. **晚到資料/修資料**  
   - 若歷史 bet 被補寫，會連鎖影響 run/trip 邊界。  
   - 需要 watermark + 小範圍重算策略（例如回滾最近 N 天）。

---

## 我建議的實作順序（低風險）
1. 先做 `run_fact`（比 trip 簡單）  
2. 把目前最重的 run-level features 搬到 `feature_run_v1`  
3. 訓練改讀 run features（先不動 trip）  
4. 再上 `trip_fact` + `feature_trip_v1`  
5. 最後導入 feature registry（版本、依賴、重算範圍）

---

你這個設計思路是對的，我唯一會強調的是：  
**把「定義版本」和「增量邊界狀態」先設好，再談 feature 豐富化**，不然很快會陷入可追溯性地獄。

