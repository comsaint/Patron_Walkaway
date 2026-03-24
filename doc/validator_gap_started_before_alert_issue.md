# Trainer vs. Validator 差異發生點

> Update (2026-03-24, Task 6): `trainer/serving/validator.py` 已移除
> `gap_started_before_alert` early-return 分支。本文保留為歷史問題說明與背景脈絡。

---

## 假設

* **10:30** 為該客人 **最後一筆下注**，且 **walkaway／`gap_start`** 與此筆對齊，故 **`gap_start` = 10:30**。
* **`compute_labels`**：觀測點 \(t\) = 該筆 `payout_complete_dtm`；**label = 1** 若 **`gap_start` ∈ \[t, t + 15 分鐘\]**（**`ALERT_HORIZON_MIN`**）。
* **Validator 結論**（**`WALKAWAY_GAP_MIN=30`**、**`LABEL_LOOKAHEAD_MIN=45`**、**`VALIDATOR_FINALIZE_ON_HORIZON=True`**。
* **`now_hk`** 已足夠晚可 **finalize**。

---

## Bets

| bet_ts | 說明 | Ground Truth (`compute_labels`) | Validator（`bet_ts` = 該筆） | **`TP`（API）** |
|--------|------|------------------|------------------------------|----------------|
| **9:55** | 首筆 | **0**（[9:55, 10:10] 不含 **10:30**） | **非** `gap_started_before_alert`（無 `last_bet_before`）；**`MISS`**（視窗內存在 **> bet_ts+15min** 之下注，例：**10:22**）→ **`result=False`** | **`"FP"`** |
| <span style="color:red"><strong>10:22</strong></span> | <span style="color:red">alert 主線</span> | <span style="color:red"><strong>1</strong>（<strong>10:30</strong> ∈ \[10:22, 10:37\]）</span> | <span style="color:red"><strong>`result=False`</strong>，<strong>`reason=gap_started_before_alert`</strong>（<strong>10:22 − 9:55 = 27 分鐘</strong> &gt; **15**；<strong>提早 `return`</strong>）</span> | <span style="color:red"><code>"FP"</code></span> |
| **10:28** | 介於 10:22 與 10:30 之間 | **1**（**10:30** ∈ \[10:28, 10:43\]） | **非** `gap_started_before_alert`（**10:28 − 10:22 = 6 分鐘**）；**L750+**（**`find_gap`**／**`MATCH`** 候選等，**finalize** 後常 **`result=True`**） | **`"TP"`** |
| **10:30** | 最後一筆(walkaway)；**`gap_start`** 對齊 | **1**（**10:30** ∈ \[10:30, 10:45\]） | **非** `gap_started_before_alert`（**10:30 − 10:28 = 2 分鐘**）；**`MATCH`** 候選 → **`result=True`** | **`"TP"`** |

---

### 逐步說明（**`bet_ts` = 10:22** ）

1. **`compute_labels`（觀測在 10:22）**：**10:30 ∈ \[10:22, 10:37\]** → **label = 1**。
2. **`validate_alert_row`（`bet_ts` = 10:22）**：**`last_bet_before` = 9:55**，**10:22 − 9:55 = 27 分鐘** &gt; **15** → 進入 **`gap_started_before_alert`** → **`result = False`**，**`return`**，**不執行**後續與 Trainer 視窗對齊之 **L750+**。
3. **對照**：同一時間軸上，**10:22** 可 **Trainer label = 1** 與 **Validator FP（協定 `TP="FP"`）** 並存；**10:28**、**10:30** 之下注因 **上一筆與 `bet_ts` 間隔 ≤ 15 分鐘**，**不會**觸發此提早分支。

---

## 分歧點（一句）

**Trainer**：只問 **\[t, t+15 分鐘\]** 內是否出現 **`gap_start`(walkaway)**。  
**Validator**：先問 **`bet_ts − last_bet_before` 是否 &gt; 15 分鐘**；若是，**直接 FP 並 `return`**，**不再**用 **`bet_ts` 之後**軌跡對齊 Trainer 視窗。

---

## 程式位置

| 項目 | 內容 |
|------|------|
| **檔案** | `trainer/validator.py` |
| **函式** | `validate_alert_row`（約 **L344** 起） |
| **分支** | **L393–L404** |

<pre><code># 摘錄自 trainer/validator.py（L393–L404）
idx = bisect_left(bet_list, bet_ts)
last_bet_before = bet_list[idx - 1] if idx &gt; 0 else None
<span style="color:red">if last_bet_before is not None and (bet_ts - last_bet_before) &gt; timedelta(minutes=15):</span>
    res_base.update(
        {
            "result": False,
            "gap_start": last_bet_before.isoformat(),
            "gap_minutes": (bet_ts - last_bet_before).total_seconds() / 60.0,
            "reason": "gap_started_before_alert",
        }
    )
    return res_base
</code></pre>

此 **`return`** 之後才是「自 **`bet_ts`** 起在視窗內找空檔」等邏輯。
