# 線上有界緩衝 K／T／D 提案（Layered Data Assets）

> **對齊**：[`ssot/layered_data_assets_run_trip_ssot.md`](../ssot/layered_data_assets_run_trip_ssot.md) **§5.4**（已發布快照 + 有界線上增量）。  
> **任務**：Execution plan **LDA-E2-07**（候選數值 + 負載評估方法；**不**在本文件凍結最終 SLO）。  
> **簽核**：候選組合與上線數值須經 **Model Owner** 與 **Ops** 書面同意後，方可寫入 implementation plan／線上設定。

---

## 1. 名詞與約束

| 符號 | 意義（§5.4） | 說明 |
|------|----------------|------|
| **K** | 線上對單一 `player_id` 最多回溯重播的 **bet 事件筆數**上界 | 與「最近 K 筆 bet」對齊；不含非 bet 之 heartbeats。 |
| **T** | 以 **`payout_complete_dtm`（或 scorer 實作對齊之事件時間）** 為準的 **曆時分鐘數**上界 | 與「最近 T 分鐘」對齊；時區須與離線 `gaming_day` 口徑一致（見 time semantics registry）。 |
| **D** | 以 **`gaming_day`** 為單位的 **曆日個數**上界 | 與「最近 D 個 `gaming_day`」對齊；邊界依 `GAMING_DAY_START_HOUR`（目前 03:00，Asia/Hong_Kong）。 |

**活躍玩家（active `player_id`）**：線上增量**僅**對活躍集合啟用有界重播；定義建議由 Ops／產品共同給出，例如「過去 7 個 `gaming_day` 內至少一筆 bet」或「目前 session 未結束之玩家」。本文件之負載上界假設活躍集合大小為 **A**。

**硬約束（來自 SSOT）**：線上**不得**對全歷史做無界重算；超出緩衝之 late／修正須走 **`late_arrival_correction_log`** 並等待下一次離線刷新（§5.4、§5.5）。

---

## 2. 候選組合（三層：保守／預設／積極）

以下為**提案**，用於容量規劃與 PoC；非生產承諾。

| 層級 | K（筆） | T（分鐘） | D（個 gaming_day） | 設計意圖 |
|------|---------|-----------|---------------------|----------|
| **保守** | 256 | 180 | 3 | 與 **trip 關閉需 3 個完整空日** 同數量級，利於線上 provisional run 與離線 trip 語義對齊之**討論**；記憶體與 CPU 峰值最低。 |
| **預設（建議 PoC 起點）** | 2_048 | 1_440（24h） | 14 | 涵蓋「高頻短窗」與「隔日再玩」常見模式；仍遠小於全歷史。 |
| **積極** | 16_384 | 10_080（7 日） | 45 | 僅在延遲 SLO 極嚴格、且硬體與 parallel 度已驗證時考慮；**筆電或單機 scorer 不建議**。 |

**同時啟用 K、T、D 時之有效窗**：對每位活躍玩家，實際納入重播之集合為三者交集（取**最嚴**子集），例如：

\[
\text{bets}_\text{online} = \{ b : b \text{ 屬於該 } player\_id \land \text{rank} \le K \land age\_\text{min} \le T \land gaming\_day \in \text{last } D \text{ days} \}
\]

實作可改為「先依 T／D 粗篩，再截斷為 K」以保證上界；**必須**在程式註解與監控中固定一種語義，避免環境間漂移。

---

## 3. 負載評估方法（可證明上界）

### 3.1 事件數上界

- 令 **A** = 同時活躍 `player_id` 數（由業務定義之上限或 P99 觀測）。  
- 單一線上重算週期內，納入重播之 bet 列數上界（粗估，忽略交集重疊時略保守）：

\[
N_\text{bets} \le A \times K
\]

若採「時間窗優先」且平均活躍玩家每秒下注率為 **r**，則另有一上界 \(N_\text{bets} \le A \times r \times 60 \times T\)（與 K 取 min 可得更緊上界）。

### 3.2 記憶體與延遲（檢核表）

1. **列寬**：以實際 scorer 讀取之欄位子集估算每列 bytes（含 Arrow／Python 物件 overhead 係數 2～4×）。  
2. **峰值 RAM**：\( \text{RAM}_\text{peak} \approx N_\text{bets} \times \text{bytes\_per\_row} \times \text{parallelism\_factor} \)。筆電 PoC 建議 **parallelism_factor = 1**，並以保守層驗證。  
3. **CPU**：run 邊界重播為 O(K) 排序 + 線性掃描量級；trip **不**強制線上全量重算（§5.5），故 trip 線上成本應主要為查表 + 輕量 provisional 欄位更新。  
4. **尾延遲**：以 P95／P99 **重播耗時** 為 SLO 輸入；若超標，優先**降 K** 或**縮 T**，再考慮降 A（活躍定義收斂）。

### 3.3 與離線刷新的關係

- 離線 **`published_snapshot_id`** 仍為該週期權威基底；K/T/D 僅覆蓋「自上次發布起、且在緩衝內」之差分。  
- 任何可能改寫**已關閉歷史 trip** 且超出緩衝之事件：**必須**入 correction log，**不得**在 serving 路徑無界重算（SSOT §5.4）。

---

## 4. 風險與未決事項

| 項目 | 說明 |
|------|------|
| **與 trainer 快取之關係** | 線上 K/T/D 與 trainer Step 6/7 快取是否共用常數，屬 **BL-04**（見 execution plan backlog）；本文件不預設合併。 |
| **跨日遲到** | D 過小可能使「僅影響舊 `gaming_day`」之 late bet 頻繁落入 correction log；需用真實 `ingest_delay` 分佈校準。 |
| **合規與稽核** | 線上 provisional 與離線定版不一致時，須保留 **`published_snapshot_id` + 線上增量版本序號**（SSOT §5.4.3）。 |

---

## 5. 簽核欄位（書面同意後填寫）

| 欄位 | 內容 |
|------|------|
| 選定組合 | ☐ 保守 ☐ 預設 ☐ 積極 ☐ 自訂：K=___ T=___ D=___ |
| 活躍玩家定義 | （連結或摘要） |
| Model Owner | 姓名／日期 |
| Ops | 姓名／日期 |
| 生效日 | YYYY-MM-DD |

---

*文件版本：提案稿 v0.1（2026-05-03）。後續升版請更新此列並同步 execution plan §6.1 **LDA-E2-07** 狀態。*
