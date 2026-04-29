# 月度延遲分布分析（t_bet / t_session / t_game）

日期：2026-04-28  
資料來源：`data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`、`data/gmwds_t_game.parquet`  
明細輸出：`analysis_outputs/delay_distribution_monthly_core_metrics.csv`

## 分析口徑

- `t_bet`：`delay_min = __etl_insert_Dtm - payout_complete_dtm`
- `t_session`：`delay_min = __etl_insert_Dtm - session_end_dtm`
- `t_game`：`delay_min = __etl_insert_Dtm - payout_complete_dtm`
- 月份分箱：以 `event_ts` 的月份（`date_trunc('month', event_ts)`）分組
- 指標：
  - 覆蓋率：`pct_le_1m`、`pct_le_7m`
  - 長尾：`pct_gt_1d`
  - 異常：`pct_neg_delay`（負延遲）、`rows_*_year_1970`、空值比例

## 整體加權結果（全歷史）

- `t_bet`：`<=1m 51.13%`、`<=7m 56.29%`、`>1d 42.33%`、`neg 0%`
- `t_session`：`<=1m 35.86%`、`<=7m 52.36%`、`>1d 44.09%`、`neg 0%`
- `t_game`：`<=1m 48.88%`、`<=7m 78.04%`、`>1d 21.08%`、`neg 34.29%`

> 這組數字與先前抽樣研究結果一致（尤其 `t_bet` 與 `t_session`）。

## 近期加權結果（最近 6 個月：2025-09 ~ 2026-02）

- `t_bet`：`<=1m 95.57%`、`<=7m 97.11%`、`>1d 0.713%`
- `t_session`：`<=1m 71.91%`、`<=7m 93.27%`、`>1d 0.7415%`
- `t_game`：`<=1m 32.71%`、`<=7m 98.08%`、`>1d 0%`

## 異常與資料品質觀察

### 1) `t_game` 存在明顯 1970 / 負延遲污染（歷史區間）

- `2024-07` 到 `2025-04` 有大量 `rows_etl_year_1970`
- 同期 `pct_neg_delay` 高達約 `85% ~ 95%`
- 這會讓 `<=1m / <=7m` 指標被「負延遲」虛高，不能直接解讀為線上可用性覆蓋率

### 2) `t_bet` / `t_session` 無 1970 ETL 問題，但歷史回補尾巴很重

- `2024-07` 到 `2025-04` 多數月份接近 `>1d = 100%`
- 顯示該區段更像歷史回補載入，不適合直接代表線上近即時延遲

### 3) `t_session` 有 `event_ts` 空值桶

- `event_month = NULL` 出現約 `89,988` 筆（`session_end_dtm` 缺失）
- 這些筆數無法計算 delay，不應納入覆蓋率分母

## 對延遲參數的含意（決策層）

- 若用「全歷史」看，`BET_AVAIL_DELAY_MIN=1` 會被回補期嚴重稀釋，導致看起來覆蓋率偏低。
- 若用「近期穩態」看：
  - `t_bet`：1 分鐘已很高（約 95%+）
  - `t_session`：1 分鐘偏緊，7 分鐘顯著更穩
  - `t_game`：1 分鐘明顯不足，7 分鐘接近完整覆蓋
- 建議以「乾淨時段」決策，不要直接用全歷史平均覆蓋率定生產參數。

## 建議後續（可直接落地）

- 產出 clean-window 版 KPI（排除 `t_game` 1970/負延遲污染月）
- 每月固定監控：
  - `pct_le_1m`、`pct_le_7m`、`pct_gt_1d`、`pct_neg_delay`
  - `rows_etl_year_1970`、`rows_event_null`
- 參數治理建議：
  - `t_bet`：可維持 1 分鐘，但需監控回退
  - `t_session`：7 分鐘較符合穩健覆蓋
  - `t_game`：7 分鐘優於 1 分鐘（近期資料已明確支持）

