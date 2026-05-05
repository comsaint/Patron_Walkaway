# Track LLM：chunk / slice 敏感特徵（高風險清單）
本文件由 `scripts/check_cum_bets_chunk_position_corr.py --emit-risk-doc` 產生；依 `trainer/feature_spec/features_candidates.yaml` 靜態掃描。
## 分級說明
- **A（嚴重）**：`ROWS … UNBOUNDED PRECEDING` — 值域強烈依賴「本 chunk 載入表內從第一筆算起」，易與「距離 chunk 起點多久」共線。
- **B（中高）**：`ROWS BETWEEN k PRECEDING` — 只看本表內最近 k **列**；在 chunk 開頭可用歷史不足，語意隨切片變形。
- **C（輕度）**：`RANGE BETWEEN INTERVAL …` — 仍以事件時間定窗，但在 chunk 最前段「實際可回溯的時間長度」較短，邊界有弱敏感。
- **D（衍生連鎖）**：`depends_on` 直接依 `cum_bets` / `cum_wager` / `avg_wager_sofar` 者，繼承 A 類風險。

## A — UNBOUNDED PRECEDING（window）
| feature_id | window_frame | description (trunc) |
|------------|--------------|------------------------|
| `cum_bets` | `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` | 該玩家至當筆累計下注筆數（canonical_id 內） |
| `cum_wager` | `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` | 該玩家至當筆累計下注金額（canonical_id 內） |

## B — ROWS k PRECEDING（window）
| feature_id | k | window_frame | description (trunc) |
|------------|---|----------------|------------------------|
| `back_bet_cnt_w20` | 20 | `ROWS BETWEEN 20 PRECEDING AND CURRENT ROW` | 近 20 把中 back bet 次數 |
| `bets_cnt_w20` | 20 | `ROWS BETWEEN 20 PRECEDING AND CURRENT ROW` | 近 20 把下注次數 |

## C — RANGE INTERVAL（window）
| feature_id | window_frame | description (trunc) |
|------------|--------------|------------------------|
| `avg_payout_odds_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘平均賠率 |
| `bets_cnt_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘下注次數 |
| `bets_cnt_w30m` | `RANGE BETWEEN INTERVAL 30 MINUTE PRECEDING AND CURRENT ROW` | 過去 30 分鐘下注次數 |
| `bets_cnt_w5m` | `RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW` | 過去 5 分鐘下注次數 |
| `lose_cnt_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘輸局次數 |
| `position_max_w30m` | `RANGE BETWEEN INTERVAL 30 MINUTE PRECEDING AND CURRENT ROW` | 過去 30 分鐘最大 position_idx |
| `position_min_w30m` | `RANGE BETWEEN INTERVAL 30 MINUTE PRECEDING AND CURRENT ROW` | 過去 30 分鐘最小 position_idx |
| `push_cnt_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘 PUSH（和局）次數 |
| `wager_avg_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘平均注碼 |
| `wager_avg_w5m` | `RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW` | 過去 5 分鐘平均注碼 |
| `wager_max_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘最大單筆注碼 |
| `wager_std_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘注碼標準差 |
| `wager_sum_w30m` | `RANGE BETWEEN INTERVAL 30 MINUTE PRECEDING AND CURRENT ROW` | 過去 30 分鐘下注總額 |
| `win_cnt_w15m` | `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW` | 過去 15 分鐘贏局次數 |

## D — derived 依賴 cum_* / avg_wager_sofar
| feature_id | depends_on | description (trunc) |
|------------|------------|------------------------|
| `avg_wager_sofar` | `cum_wager,cum_bets` | 該玩家至當筆平均注額 |
| `wager_recent_vs_session_avg` | `wager_avg_w15m,avg_wager_sofar` | 近期平均注額 vs 當次 session 平均注額比 |

## 備註
- `track_human`（例如 run boundary）也可能有 lookback/chunk 語意；本清單**僅掃 track_llm**。
- 實際訓練仍以 `trainer.training.process_chunk` 載入邊界 + DQ + identity 為準；本檔用於設計/物化前的風險盤點。
