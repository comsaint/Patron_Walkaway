# Validator 逻辑问题说明

## 依据： `trainer/validator.py`

---

## 实际逻辑（摘要）

对一则 alert，设其 **`bet_ts`** = 该则对应的下注时间。在 `validate_alert_row` 中：

1. 在该客人的下注时间序列上，找 **`bet_ts` 之前**最后一笔下注 **`last_bet_before`**。
2. 若 **`last_bet_before` 存在** 且 **`bet_ts - last_bet_before` > 15 分钟**，则：
   - 判 **`result = False`（视为误报 / FP）**
   - **`reason = "gap_started_before_alert"`**
   - **`gap_start` 设为 `last_bet_before`**
   - **`return`，不再执行**后续「从 `bet_ts` 往后看是否在观察窗内出现长空档」的完整路径。

对应原码（`trainer/validator.py`，**L393-L404**）：

```python
# L393
idx = bisect_left(bet_list, bet_ts)
# L394
last_bet_before = bet_list[idx - 1] if idx > 0 else None
# L395
if last_bet_before is not None and (bet_ts - last_bet_before) > timedelta(minutes=15):
    # L396-L403
    res_base.update(
        {
            "result": False,  # L398
            "gap_start": last_bet_before.isoformat(),  # L399
            "gap_minutes": (bet_ts - last_bet_before).total_seconds() / 60.0,  # L400
            "reason": "gap_started_before_alert",  # L401
        }
    )
    return res_base  # L404
```

亦即：**一旦进入此分支，结论已锁死为 FP。**

---

## 逻辑问题（重点）

### 1. 与直觉上的「验证问题」不一致

运营或产品上常见的验证命题是：

> **在这笔 `bet_ts` 之后**，客人在约定窗口内是否出现「够长的无下注空档」（或是否离场）。

但上述分支**完全没有**回答这个命题：它在 **`bet_ts` 之前**只看「上一笔距离 `bet_ts` 是否超过 15 分钟」，就**直接 FP**，**不再**验证 **`bet_ts` 之后**是否真的很快又进入长空档。

因此可能出现：

- **`bet_ts` 之后**客人**确实**在短时间内停止下注并形成长空档（直觉上「预测对了」），
- 但 validator **仍标 FP**，只因 **`bet_ts` 前**已有一段超过 15 分钟的空档再接上这笔下注。

这是**验证规则与「只关心 alert 之后是否发生」的语义不一致**。

### 2. 理由用语容易误导

`gap_started_before_alert` 搭配「上一笔很久以前」容易读成：

> 「walkaway／长空档在 alert 之前就已经开始了。」

但时间轴上常见情况是：**上一笔与 `bet_ts` 之间空了一段（>15 分钟），接着客人在 `bet_ts` 又下了一注**——这**不表示**「已经完成」另一套定义里的「30 分钟离场」之类结论；它只表示 **bet stream 上在 `bet_ts` 前有一段够长的 idle**。

### 3. 若下游把 `gap_start` 当「离场时间」

若 API 或报表把 **`gap_start`** 当成「实际 walkaway 时间」，在 FP + `gap_started_before_alert` 时常出现 **`gap_start`（或对应的 `walkaway_ts`）早于 `bet_ts`**。这在**时间顺序**上会让人困惑；实际上此字段此时比较像 **「用来标记 FP 理由的参考时间点」**，而非「这则 alert 所预测的离场发生时间」。

---

## 建议与原作者厘清的方向（非实现指示）

1. **当初设计意图**：此分支是要对齐某种 **offline label**，还是要测量 **「alert 之后是否发生」**？两者不同。
2. **若目标是后者**：在 **`bet_ts - last_bet_before > 15m`** 时仍应否 **early return FP**，或应继续评估 **`bet_ts` 之后**的轨迹，需要明确定义。
3. **命名与文档**：`gap_started_before_alert` 与对外字段语义是否应改名或分栏，避免与「离场时间」混淆。

---

*文档目的：向最初撰写该段逻辑的开发者对齐「为何现场会认为不合理」，并以最早 commit 为唯一历史锚点。*
