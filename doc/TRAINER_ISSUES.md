## 訓練程式碼問題追蹤（Trainer Issues Log）

本文件用來記錄 `trainer/trainer.py` 程式碼中「已識別、需修正」的問題，涵蓋「與 `doc/FINDINGS.md` 矛盾的違規項目」以及「程式碼本身的邏輯缺陷與 Train-Serve Skew」。

### 使用原則
- **問題 ID 前綴 `TRN-`**，與資料發現 `FND-` 系列互相引用但不混用。
- 若問題源自 `FINDINGS.md` 中的已知雷區，於「對應 FND」欄標注關聯 ID。
- **嚴重度定義**：🔴 P0 = 會直接污染資料/標籤/預測；🟡 P1 = 降低品質或日後維護困難；🔵 P2 = 程式碼整潔/最佳實踐。

---

## 總表：Trainer 程式碼問題

| ID | 程式碼位置 | 嚴重度 | 對應 FND | 問題描述 (Issue) | 影響 (Impact) | 建議修正 (Recommendation) |
|---|---|:---:|:---:|---|---|---|
| **TRN-01** | `trainer.py`<br>L138–148, L260–288 | 🔴 P0 | FND-01 | **Session SQL 未撈 `lud_dtm`，去重策略違反 SSOT 原則**：SQL query 完全沒有 SELECT `lud_dtm` 或 `__etl_insert_Dtm`，因此無法按 FND-01 建議以 `MAX(lud_dtm)` 選取最新版本。目前改用 `sort_values("session_end_dtm")` + `drop_duplicates(keep="last")`，無法保證挑到帳務上最新的版本。此外，`groupby(...).agg()` 已聚合成一行，其後緊接的第二次 `drop_duplicates` 為完全冗餘的操作。 | 同一 `session_id` 的事後帳務修正版本（如 `lud_dtm` 最新但 `session_end_dtm` 不變）會被錯誤排除，導致 session 特徵、標籤推導皆基於舊版本資料。 | 在 SQL 層撈取 `lud_dtm` 與 `__etl_insert_Dtm`，並以 `ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC, __etl_insert_Dtm DESC)` 去重，移除 Python 層的冗餘去重邏輯。 |
| **TRN-02** | `trainer.py`<br>L138–148, L160+ | 🔴 P0 | FND-02 | **Session query 未排除 `is_manual=1` 人工帳務調整**：SQL 沒有 `AND is_manual = 0` 條件，也沒有在 SELECT 中包含 `is_manual` 欄位，因此無法在 Python 層事後過濾。`is_manual=1` 的 Session 為 0 局數、0 Turnover，卻包含極端派彩/補償金額。 | 人工帳務 Session 被視為真實遊玩行為混入訓練集：（1）汙染 `gap_to_next_min` 計算，產生假 Walkaway 標籤；（2）扭曲 `avg_wager_sofar`、`table_hc` 等行為特徵；（3）引入極端金額拉偏模型。 | SQL 加入 `AND is_manual = 0`；若需保留 manual 作為特徵，則 SELECT 並另行分離。 |
| **TRN-03** | `trainer.py`<br>L337–345 | 🔴 P0 | FND-11 | **玩家歸戶使用 `player_id` 而非 `casino_player_id`**：`gap_to_next_min`（走牌標籤的核心計算）使用 `groupby("player_id")` 排序並計算下一場 Session 的開始時間。FND-11 指出 `player_id` 與 `casino_player_id` 存在 M:N 多對多映射，換卡或系統重發 ID 的玩家在不同 `player_id` 下，其 Session 鏈結會被強制切斷。 | 同一位玩家換卡後的 Session 被分裂到不同的 group，`gap_to_next_min` 被誤算為極大值（如幾天），大量製造假 Walkaway 正例，標籤直接不可信。 | 以 `casino_player_id`（優先）歸戶計算 gap；無卡客才退而求其次使用 `player_id`。需先清洗字串 `'null'`（參考 FND-03）。 |
| **TRN-04** | `trainer.py`<br>L160+ | 🟡 P1 | FND-12 | **未排除 Dummy ID 假帳號（過客/伴遊 ID）**：沒有對「1 次 Session 且局數 ≤ 1」的 8 位數純數字 `casino_player_id` 做任何過濾。 | FND-12 指出此類 Dummy ID 佔活躍會員約 4%，幾乎不下注就離開，會嚴重稀釋 Walkaway 標籤分佈，讓模型學到「所有人都很快離開」的錯誤模式。 | 在特徵工程前，依 `casino_player_id` 統計 `session_cnt` 與 `total_games`，過濾掉 `session_cnt == 1 AND total_games <= 1` 的 ID。 |
| **TRN-05** | `trainer.py`<br>L290–334 | 🔴 P0 | FND-13 | **`table_hc`（同桌人數）使用了線上不可得的 `session_end_dtm`，造成未來資料外洩**：程式碼利用 `sessions_df` 的 `session_end_dtm` 重建整張桌台的佔用時間線，以此計算每筆注單當下同桌人數。但 FND-13 明確指出：`t_session` 是在牌局**結束後**才完整入湖，線上推論的時間點 T，尚未結束的同桌玩家根本不存在 `session_end_dtm`。 | 訓練時模型看到了事後才知道的「完整桌台人數」，線上推論時卻只能看到已離開玩家的記錄，形成嚴重的 Training-Serving Skew，模型依賴一個線上根本拿不到的特徵。 | 線上推論以「已有 `session_end_dtm` 的已結束 Session」估算同桌人數（下界近似），或改用截至當前注單時間點已可見的 Session（`event_time = COALESCE(session_end_dtm, lud_dtm) + 7min` 作為可用時間）。 |
| **TRN-06** | `trainer.py`<br>L384–390 | 🔴 P0 | — | **資料窗口末端 Session 的 Walkaway 標籤必然為正例（標籤膨脹）**：`gap_to_next_min` 為 NaN 的 Session（每位玩家在訓練窗口中的最後一個 Session），被一律填充為 `1e9`，因此**必定**滿足 `gap >= 30` 條件，被標記為 Walkaway。然而玩家可能在窗口結束後不久即返回，只是未被納入本次訓練資料。 | 每位活躍玩家至少有一個 Session 被錯誤標記為 Walkaway，直接膨脹 Positive Rate 並引入大量標籤雜訊，尤其在玩家基數大時影響顯著。 | 策略一：不對窗口結束前 N 小時（如 2 小時）內結束的 Session 標記標籤（設為 NaN 並在訓練時忽略）。策略二：SQL 多拉窗口結束後額外 1 天的 Session，僅用於計算 gap，不加入訓練樣本。 |
| **TRN-07** | `trainer.py`<br>L182–243 | 🔴 P0 | — | **Rolling cache 沒有日期範圍一致性檢查，可能靜默地使用舊資料訓練**：只要本機存在 `rolling_cache.csv` 或 `features_buffer.csv`，無論本次訓練的 `start/end` 日期窗口是否與 cache 一致，都會直接使用，並且把傳入的 `bets_df` 完全丟棄。 | 觸發此路徑時，模型實際上是在上一次訓練的舊資料集上重訓，但日誌顯示的是新的 `start/end` 日期，開發者完全無感。這是靜默失效的 P0 資料一致性問題。 | Cache 檔案應包含 `start/end` 元資料，讀取時驗證是否與當次請求的日期窗口相符；不符則強制重新抽取並更新 cache。 |
| **TRN-08** | `trainer.py`<br>L193–242<br>`scorer.py`<br>L355–379 | 🔴 P0 | — | **Rolling window 計算語義不一致（Train-Serve Skew）**：Trainer 使用 pandas `rolling(f"{window}min")`，其語義為含邊界（inclusive）的時間窗口；Scorer 使用手動 numpy 迴圈，以 `while t - ts[start] > win_ns` 實作嚴格排除左邊界的語義。兩者在邊界上的注單計數可能相差 1。 | 模型在訓練時學習到的特徵分佈（邊界注單被計入），與線上推論時實際計算的特徵（邊界注單被排除）不一致，直接破壞模型的泛化能力。 | 統一兩端邏輯：建議 Trainer 改用 Scorer 的 numpy 實作（效率更佳且語義明確），確保邊界行為一致。 |
| **TRN-09** | `trainer.py`<br>L412 | 🟡 P1 | — | **`loss_streak`（連敗次數）計算邏輯失效**：程式碼以 `isinstance(st, str) and st.upper() == "LOSE"` 判斷輸局，但 `t_bet` 的 `status` 欄位在實際使用中並非字串類型（或值域不為 `"LOSE"`），導致此條件永遠為 False，`loss_streak` 特徵全程為 0。 | 連敗特徵對 Walkaway 預測可能有重要預測力（連敗後玩家更傾向離桌），但此特徵等同於無效特徵，模型學不到任何連敗資訊。 | 驗證 `t_bet.status` 的實際值域（如 `WIN`/`LOSE`/`TIE` 或其他編碼），修正比較邏輯或欄位來源。 |
| **TRN-10** | `trainer.py`<br>L348–362 | 🔵 P2 | — | **Merge 操作產生欄位名稱碰撞**：`bets_df` 已含 `player_id` 與 `table_id`，與 `sessions_df` merge 後產生 `player_id_x`、`player_id_y`、`table_id_x`、`table_id_y`，後續程式碼若引用 `merged["player_id"]` 行為不明確。 | 雖然這些碰撞欄位不在 `feature_cols` 中，不直接汙染模型輸入，但會讓後續除錯困難，且存在日後誤引用的隱患。 | Merge 時僅帶入 sessions 側需要的欄位：`["session_id", "session_start_dtm", "session_end_dtm", "gap_to_next_min"]`，排除 bets 側已有的欄位。 |
| **TRN-11** | `trainer.py`<br>L536–566 | 🟡 P1 | — | **Threshold 選擇邏輯偏向極端 Precision，實際 Recall 趨近於 `min_recall` 下限**：Threshold 從低到高遍歷，最終選出 Precision 最高的點（對應最高 Threshold），其 Recall 通常僅略高於 `min_recall=2%`。Fallback 路徑的選擇邏輯亦相同。 | 線上部署後幾乎不產生任何 Alert（高 Precision、極低 Recall），系統從客觀上無法發揮 Walkaway 預警的業務功能。 | 引入業務導向的 F-beta score（`beta > 1`，偏重 Recall）或設定最低 Recall 閾值（如 10%），以業務可接受的誤報率為上限尋找最大 Recall 的 Threshold。 |
| **TRN-12** | `trainer.py`<br>L509–512 | 🔵 P2 | — | **`train_and_select_model` 實際上沒有多模型比較**：`default_lgb_grid` 中第二組參數被完整註解掉，Grid search 只跑一組 LightGBM 超參數，函式名稱與實際行為不符。 | 不影響模型功能，但誤導後人以為此函式做了模型選擇，實際上是直接訓練唯一一組參數。 | 恢復 Grid 中的多組參數，或將函式更名為 `train_model` 以如實反映其行為。 |
| **TRN-13** | `trainer.py`<br>L89–92 | 🔵 P2 | — | **模組 Docstring 位置錯誤**：檔案開頭說明字串被放在 `save_rolling_cache()` 函式定義之後、`load_clickhouse_data()` 之前，在 Python 中不構成模組 docstring，僅為一個被直譯器忽略的字串表達式。 | 不影響功能，但 `trainer.__doc__` 為 None，IDE 工具提示無法顯示模組說明。 | 將 docstring 移至檔案第一行（所有 import 之前）。 |
| **TRN-14** | `trainer.py`<br>L649–658 | 🟡 P1 | — | **`main()` 中 `start`/`end` 變數語義依路徑而不一致**：使用 ClickHouse 路徑時，`start`/`end` 為 `parse_window()` 回傳的 tz-aware datetime；使用本機 CSV 路徑時，`start`/`end` 被賦值為 pandas Timestamp（可能 tz-naive）。若同時走本機資料 + Feature Cache 路徑，bets/sessions 資料被載入後完全未被使用，造成不必要的記憶體消耗。 | tz-aware/naive 不一致可能在比較或序列化時拋出例外，且資料載入後被靜默丟棄是潛在的效能浪費與邏輯混淆。 | 統一 `start`/`end` 的時區處理（一律 tz-aware 或 tz-naive）；若走 Feature Cache 路徑則跳過 bets/sessions 的資料庫查詢。 |

---

## 附錄：問題驗證程式碼（Evidence）

### [TRN-01] Session 去重：確認目前邏輯與 SSOT 的差異

```python
# 模擬現有邏輯：用 session_end_dtm 排序後去重
sessions_wrong = (
    sessions_df
    .sort_values(["session_id", "session_end_dtm"])
    .groupby("session_id", as_index=False)
    .agg({"player_id": "first", "session_end_dtm": "max"})
    # 第二次 drop_duplicates 為冗餘操作（groupby 已保證每 session_id 唯一）
    .drop_duplicates(subset=["session_id"], keep="last")
)

# 建議修正：在 SQL 層使用 ROW_NUMBER() 去重
# 等效 ClickHouse SQL：
sql_dedup = """
SELECT *
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY session_id
               ORDER BY lud_dtm DESC, __etl_insert_Dtm DESC
           ) AS rn
    FROM gmwds.t_session
    WHERE session_start_dtm >= %(start)s
      AND is_manual = 0
)
WHERE rn = 1
"""
```

### [TRN-02] 驗證 `is_manual` 對訓練資料的污染程度

```python
import pandas as pd

# 讀取含 is_manual 欄位的 session 資料（需重新撈取）
sessions_with_manual = pd.read_parquet("data/gmwds_t_session.parquet",
                                       columns=["session_id", "is_manual",
                                                "num_games_with_wager", "turnover",
                                                "player_win"])
print(sessions_with_manual.groupby("is_manual").agg(
    rows=("session_id", "count"),
    zero_games_pct=("num_games_with_wager", lambda x: (x == 0).mean()),
    mean_abs_player_win=("player_win", lambda x: x.abs().mean()),
))
```

### [TRN-03] 驗證 `player_id` 歸戶切斷產生的假 Walkaway 標籤

```python
# 找出同一位玩家有多個 player_id 的案例
import duckdb

duckdb.sql("""
SELECT
    clean_casino_player_id,
    COUNT(DISTINCT player_id) AS num_player_ids,
    COUNT(DISTINCT session_id) AS total_sessions,
    MIN(session_start_dtm) AS first_session,
    MAX(session_start_dtm) AS last_session
FROM (
    SELECT player_id, session_id, session_start_dtm,
           CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL
                ELSE trim(casino_player_id) END AS clean_casino_player_id
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE is_manual = 0
)
WHERE clean_casino_player_id IS NOT NULL
GROUP BY 1
HAVING num_player_ids > 1
ORDER BY num_player_ids DESC
LIMIT 20
""")
```

### [TRN-06] 估算窗口末端「必然正例」的標籤膨脹比例

```python
# 假設 labeled_df 已建立完畢
last_sessions = labeled_df.groupby("player_id")["session_start_dtm"].transform("max")
is_last = labeled_df["session_start_dtm"] == last_sessions

print("末端 Session 數量:", is_last.sum())
print("末端 Session 中標記為 Walkaway 的比例:",
      labeled_df.loc[is_last, "label"].mean())
print("全體 Walkaway 比例:", labeled_df["label"].mean())
print("末端 Session 佔全體 Walkaway 正例的比例:",
      labeled_df.loc[is_last & (labeled_df["label"] == 1)].shape[0]
      / labeled_df["label"].sum())
```

### [TRN-07] 驗證 Cache 路徑是否使用了錯誤日期範圍的資料

```python
import os, json, pandas as pd
from pathlib import Path

CACHE_META = Path("cache/rolling_meta.json")  # 建議新增的 metadata 檔

def rolling_cache_is_valid(start, end):
    """建議加入此防衛性檢查"""
    if not CACHE_META.exists():
        return False
    meta = json.loads(CACHE_META.read_text())
    cached_start = pd.Timestamp(meta["start"])
    cached_end   = pd.Timestamp(meta["end"])
    return cached_start == start and cached_end == end

# 呼叫方式：
# if rolling_cache_exists() and rolling_cache_is_valid(start, end):
#     bets_df = load_rolling_cache()
# else:
#     bets_df = fetch_from_clickhouse(start, end)
#     save_rolling_cache(bets_df, meta={"start": start.isoformat(), "end": end.isoformat()})
```

### [TRN-08] 驗證 Rolling Window 邊界語義不一致

```python
import pandas as pd
import numpy as np

# 建立含邊界注單的測試案例
ts = pd.to_datetime(["2024-01-01 10:00:00",
                     "2024-01-01 10:05:00",
                     "2024-01-01 10:10:00"])  # 邊界點
values = [1, 2, 3]
df = pd.DataFrame({"ts": ts, "val": values}).set_index("ts")

# Trainer 做法（pandas rolling，含邊界）
trainer_result = df["val"].rolling("10min").sum()

# Scorer 做法（numpy 嚴格排除左邊界）
ts_ns = ts.astype(np.int64).values
win_ns = 10 * 60 * 1_000_000_000
counts = np.zeros(len(ts_ns), dtype=np.float64)
start = 0
for i in range(len(ts_ns)):
    while ts_ns[i] - ts_ns[start] > win_ns:
        start += 1
    counts[i] = i - start + 1

print("Trainer (inclusive):", trainer_result.values)
print("Scorer  (exclusive) :", counts)
# 邊界點（10:10:00）距 10:00:00 剛好 10 分鐘，兩者結果不同
```

### [TRN-09] 驗證 `loss_streak` 特徵是否全為 0

```python
# 在訓練資料建立後執行
print("loss_streak 值域：", labeled_df["loss_streak"].unique())
print("loss_streak 全為 0？", (labeled_df["loss_streak"] == 0).all())

# 同時驗證 t_bet 的 status 欄位實際值域
import duckdb
duckdb.sql("""
SELECT status, COUNT(*) as cnt
FROM read_parquet('data/gmwds_t_bet.parquet')
GROUP BY 1
ORDER BY cnt DESC
LIMIT 20
""")
```
