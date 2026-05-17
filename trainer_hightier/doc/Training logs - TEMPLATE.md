# Training Logs - TEMPLATE (精簡版 v1)

本文件定義訓練流程輸出的 3 個 log 檔模板，目標是：
- 讓 run 可比較（尤其是資料抽樣與 Optuna 預算）
- 讓業務解讀與工程除錯分離
- 降低重複欄位與環境綁定資訊

---

## 1) `run_summary.json`（比較主檔，必看）

用途：跨 run 比較時唯一必讀檔，欄位應短且穩定。

```json
{
  "run_id": "20260517-015601-891d420",
  "started_at": "2026-05-17T01:56:01Z",
  "finished_at": "2026-05-17T03:52:21Z",
  "duration_sec": 6980.2,

  "data_scope": {
    "population_scope": "rated_patrons",
    "patron_sampling_method": "hash",
    "patron_sampling_ratio": 0.1,
    "patron_sampling_key": "patron_id",
    "n_unique_patrons_train": 12345,
    "n_unique_patrons_val": 4567,
    "n_unique_patrons_test": 4321
  },

  "model": {
    "algorithm": "lightgbm",
    "n_features_used": 30,
    "feature_list_hash": "sha256:abc..."
  },

  "thresholding": {
    "policy": "min_precision",
    "policy_param": {
      "min_precision": 0.6
    },
    "selected_threshold": 0.5589
  },

  "metrics": {
    "val": {
      "ap": 0.5031,
      "precision": 0.6000,
      "recall": 0.0948,
      "f1": 0.1638,
      "samples": 2569944,
      "positives": 974745,
      "alerts": 154058,
      "alerts_per_hour": 63.5108
    },
    "test": {
      "ap": 0.4959,
      "precision": 0.5939,
      "recall": 0.0904,
      "f1": 0.1569,
      "samples": 2397561,
      "positives": 887969,
      "alerts": 135132,
      "alerts_per_hour": 56.4887
    }
  },

  "optimization": {
    "enabled": true,
    "backend": "optuna",
    "max_time_sec_configured": 1800,
    "max_trials_configured": 200,
    "wall_time_sec_actual": 1693.2,
    "trials_completed": 121,
    "stopping_reason": "time_budget_exhausted",
    "best_value": 0.0948
  }
}
```

### `run_summary.json` 必填欄位（精簡版）
- `run_id`
- `data_scope.patron_sampling_ratio`（解決 1%/10% 可追溯問題）
- `thresholding.selected_threshold`
- `metrics.val` + `metrics.test` 的 `ap/precision/recall/f1`
- `optimization.max_time_sec_configured`
- `optimization.wall_time_sec_actual`
- `optimization.trials_completed`
- `optimization.stopping_reason`

---

## 2) `metrics_detailed.json`（分析檔，給模型調參/報表）

用途：保存完整指標與曲線點位，不塞進 summary。

```json
{
  "run_id": "20260517-015601-891d420",
  "split_metrics": {
    "train": {
      "ap": 0.5382,
      "precision": 0.6279,
      "recall": 0.1349,
      "f1": 0.2220
    },
    "val": {
      "ap": 0.5031,
      "precision": 0.6000,
      "recall": 0.0948,
      "f1": 0.1638
    },
    "test": {
      "ap": 0.4959,
      "precision": 0.5939,
      "recall": 0.0904,
      "f1": 0.1569
    }
  },
  "threshold_analysis": {
    "selection_policy": "min_precision=0.6",
    "selected_threshold": 0.5589
  },
  "budget_points": {
    "alerts_per_hour": {
      "val": 63.5108,
      "test": 56.4887
    }
  },
  "feature_columns": [
    "wager",
    "casino_win",
    "is_back_bet"
  ]
}
```

---

## 3) `pipeline_debug.json`（工程除錯檔）

用途：保留流程、cache、耗時、例外診斷；不參與業務比較。

```json
{
  "run_id": "20260517-015601-891d420",
  "cache": {
    "session_clean_cache_hit": true,
    "bet_base_clean_cache_hit": true,
    "bet_segment_clean_cache_hit": false
  },
  "timings_sec": {
    "prepare_training_frame": 207.659,
    "build_training_dataset": 315.738,
    "step4": 88.579,
    "step5": 6016.396,
    "run_training_total": 6920.172
  },
  "resource_usage": {
    "peak_ram_mb": 0,
    "cpu_time_sec": 0
  },
  "artifacts": {
    "model_path": "out/models_high_tier_mvp/20260517-015601-891d420/model.pkl",
    "training_metrics_path": "out/models_high_tier_mvp/20260517-015601-891d420/training_metrics.json"
  },
  "errors": []
}
```

---

## Include / Exclude 規則（v1）

### Include（建議保留）
- 與可比較性直接相關：抽樣比例、threshold policy、核心 metrics、Optuna 時間預算與實際消耗
- 與 debug 直接相關：各階段耗時、cache 命中、錯誤訊息

### Exclude（建議移出 summary）
- 重複欄位（例如同義 path 重複）
- 機器綁定絕對路徑（`C:\\Users\\...`）；改為相對路徑
- 過度細節且不影響比較的工程內部欄位

---

## 命名與落盤建議
- 每次 run 產生三檔：
  - `run_summary.json`
  - `metrics_detailed.json`
  - `pipeline_debug.json`
- 與 `model.pkl` 同層保存，避免跨目錄查找成本。
- 欄位新增時採 backward-compatible（只新增不改名），每季再做一次 schema 清理。
