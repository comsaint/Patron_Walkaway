# High-Tier MVP 模型結果一頁摘要

## 比較對象
- Run A: `out/models_high_tier_mvp/20260516-220139-4ef27ad`
- Run B: `out/models_high_tier_mvp/20260517-015601-891d420`
- 兩次皆使用 30 個 baseline features、相同最小 precision 約束（`min_precision=0.6`），並由驗證集挑選 threshold。

## 模型表現重點（聚焦 Validation/Test）

### 1) 在相同 precision 目標下，Run B 召回率明顯提升
- **Validation**
  - Run A: Precision `0.6000`, Recall `0.0322`, F1 `0.0612`, AP `0.4374`
  - Run B: Precision `0.6000`, Recall `0.0948`, F1 `0.1638`, AP `0.5031`
- **Test**
  - Run A: Precision `0.5987`, Recall `0.0330`, F1 `0.0625`, AP `0.4385`
  - Run B: Precision `0.5939`, Recall `0.0904`, F1 `0.1569`, AP `0.4959`
- 結論：Run B 在 precision 幾乎持平（略降）的情況下，把 recall 與 F1 拉高約 2-3 倍，AP 亦明顯上升，整體辨識能力更好。

### 2) Threshold 變化
- Run A threshold: `0.5688`
- Run B threshold: `0.5589`
- Run B 門檻略低，配合模型分數分布，帶來更多告警量與更高召回。

### 3) 告警量與資料規模差異（解讀時要注意）
- **Test alerts**: Run A `6,153` vs Run B `135,132`
- **Test alerts/hour**: Run A `2.57` vs Run B `56.49`
- **Test samples**: Run A `367,995` vs Run B `2,397,561`
- Run B 的資料量和正例量都遠高於 Run A，因此指標進步不一定完全來自模型學習能力，也可能含資料覆蓋變化效應。建議下一步做固定時間窗或固定抽樣的 A/B 重跑，確認純模型增益。

### 4) 訓練效率與資源影響
- `step5_seconds`: Run A `3,718.7s` -> Run B `6,016.4s`（約 +62%）
- `build_training_dataset_seconds`: Run A `60.2s` -> Run B `315.7s`
- Run B 表現較佳，但訓練時間與資料處理成本顯著提高；若在筆電環境反覆訓練，需留意執行時間與記憶體壓力。

## 使用特徵（30 個）與其價值

### A. 原始交易與類別特徵
- `wager`, `casino_win`, `is_back_bet`, `bet_type`, `type_of_bet`
- 作用：提供單筆下注金額、盈虧、玩法型態等直接訊號，是最基礎的行為語意來源。

### B. 短窗行為聚合（1h / 15m / 1d）
- 例：`bet__bets_cnt__w1h`, `bet__wager_sum__w1h`, `fe__wager_sum__w15m`, `fe__bets_cnt__w15m`, `fe__bets_cnt__w1d`, `fe__wager_sum__w15m_over_w1d`
- 作用：捕捉「短期爆量」或「近期強度相對於日常」的變化，對偵測突發風險非常關鍵。

### C. 玩家長週期輪廓（180d snapshot）
- `patron__theo_win_sum__w180d_m1snap`, `patron__gaming_days_cnt__w180d_m1snap`, `patron__adt__w180d_m1snap`
- 作用：建立玩家基準線，讓模型區分「高活躍常態玩家」與「異常升溫行為」。

### D. 當日 canonical 進度特徵
- `fe__canonical__bets_cnt__today`, `fe__canonical__wager_sum__today`, `fe__canonical__avg_wager__today`, `fe__canonical__elapsed_sec_since_first_bet__today`
- 作用：描述當日累積軌跡（次數、金額、節奏），有助於判斷是否偏離同日常態路徑。

### E. 節奏與間隔異常（interarrival）
- `fe__interarrival__lag2_sec`, `fe__interarrival__last_gap_z__w7d`, `fe__interarrival__last_gap_to_recent_mean_ratio__w1h`, `fe__interarrival__cv__w1h`
- 作用：把下注節奏視為時間序列，抓取忽快忽慢、密集連發等異常節律。

### F. 賠率行為特徵（odds）
- `bet__payout_odds_avg__w1h`, `fe__payout_odds_z_prior_w30d`, `fe__odds__payout_odds_z__w1h`, `fe__odds__payout_odds_z__w7d`, `fe__odds__payout_odds_to_recent_max_ratio__w1h`, `fe__odds__payout_odds_step_ratio`
- 作用：監控賠率選擇是否偏離歷史習慣，對辨識策略改變或風險偏好切換很有幫助。

## 結論與建議
- 若目標是「在 precision ~0.6 下最大化抓取率」，**Run B 明顯優於 Run A**。
- 但兩次資料規模差異非常大，為了避免誤判，建議補做「同資料切片」的公平比較（固定時間窗 + 固定抽樣 + 相同 Optuna trial budget）。
- 若部署端對告警量有上限，應再做 threshold calibration（例如依每小時 alert budget 校準），避免 Run B 的高告警量衝擊人力或下游系統。
