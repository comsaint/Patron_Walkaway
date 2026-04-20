# 錯誤切片分析總結（Frozen T0 · 365d lookback）

> **產物對照**：`seg_frozen_365d_default.json`（與本摘要數字同源）  
> **產出日期**：2026-04-21  
> **對齊衝刺文件**：`PLAN_precision_uplift_sprint.md` Phase 1「錯誤切片分析」— 探索用口徑，**非**嚴格 PIT 與訓練 Gate 的唯一契約。

---

## 1. 評估設定（可重現）

| 項目 | 值 |
|------|-----|
| 評估來源 | `backtest_predictions.parquet`（rated eval 列） |
| 檔案路徑 | `out/backtest/20260419-040815-6ec219f_post_train/backtest_predictions.parquet` |
| 時間窗 | `start_ts`：`2026-04-01T00:00:01+08:00` → `end_ts`：`2026-04-20T00:00:00+08:00` |
| 告警門檻 | `score >= 0.8506701249724581`（`training_metrics.json` → `rated.threshold_at_recall_0.01`） |
| 切片 profile | **Frozen**：`--frozen-segments-from-session-parquet` + **365d** lookback；`player_id→canonical_id` 由 **`trainer.identity.build_canonical_mapping_from_df`**（與 backtester 同源） |
| Session 聚合 T0（naive UTC） | JSON notes：`2026-03-31 16:00:01`（由 `--start-ts` 換算） |

---

## 2. 整體規模與資料覆蓋（必讀限制）

| 指標 | 數值 |
|------|------|
| Eval 列總數 | 12,929,873 |
| 進入切片後保留列數 | 11,227,524 |
| 因「無 session 聚合 profile」丟棄 | **1,702,349**（`missing_profile_for_canonical_id`） |
| 全域 `error_rate`（保留列） | **≈ 0.1346** |

**Notes 摘要（session 側）**

- 自動 canonical map：**338,817** 列 `player_id→canonical_id`。
- 於 T0 前 365d 內、且能與 eval `canonical_id` 對上並通過 DQ 的 session 聚合：**20,149** 個 CID 有聚合列。
- 本次 eval 去重 CID 請求數：**27,919**（仍有部分 CID 在 session 檔／join 後無可用聚合 → 大量列被丟）。

**解讀**：本 run 的切片結論 **強依賴「session parquet 與 eval 的覆蓋交集」**；丟棄列佔比高時，**不可**把切片當成「全母體無偏估計」，但仍可用於 **Phase 1 排優先與假說**。

---

## 3. 指標定義（與 JSON 欄位一致）

- **`n`**：該切片內的 **eval 列數**（非累加；同一玩家多筆 bet 會重複計入）。
- **`precision_at_alert`**：`tp / (tp + fp)`，僅在該切片內有告警時有意義。
- **`error_rate`**：`error_count / n`（`error` = `is_alert != label`）。
- **`alert_rate`**：`alerts / n`。

各維度列表在 JSON 內依 **`precision_at_alert` 升序**（再比 `error_rate`）排序。

---

## 4. 維度層級：哪類切片最有「單獨處理」潛力（主觀排序）

由高到低（兼顧 **可路由性** × **與模型/特徵工程相關性**）：

1. **`adt_percentile_bucket` / `activity_percentile_bucket` / `turnover_30d_percentile_bucket`**（行為強度三軸；frozen 下 decile 在 **每個 canonical 一點** 上計算後廣播到列）
2. **`tenure_bucket`**（生命週期；`T1` 明顯偏弱）
3. **`eval_date`**（日級診斷／營運或標籤異常排查）
4. **`table_id`**（局部 hotspot；多為 **小 n**，不適合當唯一全域主軸）

---

## 5. ADT 分桶（`adt_d1` = 全體有 ADT 者中 ADT 最低約 10% 等分；**非**累加 n）

| 分桶 | n | precision@alert | error_rate | alert_rate |
|------|---:|----------------:|-----------:|-----------:|
| adt_unknown | 168,340 | 0.358 | 0.147 | 0.00770 |
| adt_d5 | 834,227 | 0.394 | 0.151 | 0.00341 |
| adt_d7 | 1,360,464 | 0.407 | 0.130 | 0.00194 |
| adt_d4 | 666,349 | 0.411 | 0.164 | 0.00512 |
| adt_d8 | 1,554,858 | 0.415 | 0.128 | 0.00161 |
| adt_d6 | 1,092,038 | 0.422 | 0.142 | 0.00220 |
| adt_d2 | 354,950 | 0.431 | 0.202 | 0.0143 |
| adt_d9 | 1,937,790 | 0.434 | 0.120 | 0.00123 |
| adt_d3 | 490,499 | 0.438 | 0.183 | 0.00856 |
| adt_d10 | 2,514,937 | 0.447 | 0.109 | 0.000827 |
| adt_d1 | 253,072 | **0.497** | 0.213 | **0.0279** |

**白話**：`unknown` 與中段多桶 **亮燈時較不準**；**d1** 亮燈時相對準但 **告警多、整體錯誤率也高**；**d10** 告警少、錯誤率較低。各桶 **n 不均** 為預期（列數加總 + 玩家重複下注）。

---

## 6. Activity／Turnover（節錄：最差與較佳）

**Activity（節錄）**

| 分桶 | n | precision@alert |
|------|---:|----------------:|
| activity_unknown | 168,340 | 0.358 |
| activity_d4 | 878,990 | 0.398 |
| activity_d1 | 450,390 | 0.400 |
| … | … | … |
| activity_d9 | 1,581,284 | 0.481 |
| activity_d10 | 2,015,715 | 0.486 |
| activity_d7 | 1,281,043 | **0.493** |

**Turnover（節錄）**

| 分桶 | n | precision@alert |
|------|---:|----------------:|
| to_unknown | 168,340 | 0.358 |
| to_d5 | 876,379 | 0.405 |
| to_d4 | 662,947 | 0.418 |
| … | … | … |
| to_d10 | 2,510,682 | 0.473 |
| to_d8 | 1,614,255 | **0.487** |

（完整列請見 JSON `segments.activity_percentile_bucket` / `turnover_30d_percentile_bucket`。）

---

## 7. Tenure

| 分桶 | n | precision@alert | alert_rate |
|------|---:|----------------:|-----------:|
| **T1**（約 8–30 天） | 423,049 | **0.369** | 0.00580 |
| T3 | 9,643,152 | 0.436 | 0.00295 |
| T2 | 934,600 | 0.447 | 0.00405 |
| T0_seg（≤7 天） | 226,723 | 0.495 | 0.00528 |

**結論**：**`T1` 是最值得單獨做路由／特徵／門檻實驗的 tenure 段**。

---

## 8. Eval date（節錄：最差 vs 較佳）

| eval_date | n | precision@alert |
|-----------|---:|----------------:|
| 2026-04-16 | 707,299 | 0.404 |
| 2026-04-14 | 684,828 | 0.415 |
| … | … | … |
| 2026-04-17 | 551,426 | 0.455 |
| 2026-04-12 | 737,775 | **0.464** |

**用途**：偏 **營運／日級資料品質／混桌** 排查；不宜取代玩家行為主軸。

---

## 9. Table id（性質說明）

列表前段常見 **`precision_at_alert = 0` 且 `n` 僅數百～數千** 的桌台（例如 `31351001`、`24051001` 等）。

**用途**：**Hotspot 深挖**（規則、標籤、桌況）；**不**宜作為唯一全域分群維度。

---

## 10. ADT 數值邊界（`decile_bounds`）

目前 repo 內 **`analyze_segment_error_rates.py` 已支援** 在 JSON 根節點輸出 **`decile_bounds`**（各維度每桶 `min` / `max` / `n_decile_sample` 及相鄰桶 cutpoint）。

**本檔所依之 `seg_frozen_365d_default.json` 若無 `decile_bounds` 欄位**，代表該 JSON 為較早產物；請以最新腳本重跑並覆寫輸出即可取得邊界表。

---

## 11. 與「precision > 60%」目標的關係（誠實結論）

- 各維度切片顯示：**多數大桶的 `precision_at_alert` 仍落在約 0.36–0.50 區間**，與整體「約四成上下」同一量級。
- **分群建模（ADT / activity / turnover / tenure）值得做**，較像 **中等幅度 uplift** 的主戰場；**單靠「每段各練一個模型」就指望跨全域到 >60%，機率偏低**，通常還要搭配 **標籤／資料契約、hard-negative、門檻與目標函數** 等。

---

## 12. 建議後續動作（工程／分析）

1. **縮小「無 session 聚合」丟列**：補齊 session 覆蓋或釐清 CID 對齊；否則切片只代表子母體。  
2. **優先實驗路由**：`activity`×`turnover` 弱桶 + `T1` + 中段 ADT。  
3. **重跑產物**：使用含 **`decile_bounds`** 的腳本版本輸出 JSON，將數值邊界併入實驗設計與文件。  
4. **與 §7 `slice_contract` 分工**：本 frozen 報告適合 **Phase 1 假說**；正式 Gate 仍以計畫書契約為準。

---

## 附錄：重跑指令（範例）

於 repo 根目錄：

```bash
PYTHONPATH=. python investigations/precision_uplift_recall_1pct/phase1/analyze_segment_error_rates.py \
  --backtest-predictions-parquet out/backtest/20260419-040815-6ec219f_post_train/backtest_predictions.parquet \
  --start-ts 2026-04-01T00:00:01+08:00 \
  --end-ts 2026-04-20T00:00:00+08:00 \
  --frozen-segments-from-session-parquet data/gmwds_t_session.parquet \
  --output-json investigations/precision_uplift_recall_1pct/phase1/seg_frozen_365d_default.json
```

（`--frozen-segment-lookback-days` 預設為 **365**；若要 30 天可顯式傳入。）
