# Feature Spec YAML 指南（Track Profile / Track LLM / Track Human）

本文件說明 **Feature Spec YAML** 的角色、結構與使用方式，並提供一段「給未來 LLM 看的 Prompt」範例，方便之後自動產生 Track LLM 候選特徵。

> 關鍵原則：  
> - **人類決定語義與護欄**（哪些欄位可以用、允許哪些函數、視窗長度上限）。  
> - **LLM 協助發想 expression + window_frame**，但不寫整段 SQL。  
> - **工程程式負責組裝 SQL、注入 cutoff / PIT / 防洩漏邏輯並做靜態驗證**。

---

## 1. 檔案位置與角色

- 候選特徵定義（spec 原始檔）：  
  - `trainer/feature_spec/features_candidates.yaml`（實際使用時由 template 複製或改名自 `.template.yaml`）  
- 生產特徵清單（active features）：  
  - `trainer/feature_spec/feature_list.json` 或 `features_active.yaml`（由訓練流程產生）

兩者關係：

1. **features_candidates.yaml**：Track Profile / Track LLM / Track Human 的「特徵全集」，包含所有候選。  
2. 訓練流程（`trainer.py`）讀取 candidates，計算特徵並做 Feature Screening。  
3. Screening 結果寫入 **feature_list.json**（只留需要真的計算與送入模型的 feature_id）。  
4. `scorer.py` 只依據 feature_list.json + candidates spec 來計算線上特徵。

---

## 2. YAML 結構概要

完整模板見：

```text
trainer/feature_spec/features_candidates.template.yaml
```

### 2.1 全域區塊

- `version` / `spec_id` / `description`：人類可讀版本與識別碼。  
- `tracks_enabled`：三軌開關（Track Profile / Track LLM / Track Human）。  
- `execution`：時間欄位、分組鍵與排序欄位（`payout_complete_dtm ASC, bet_id ASC`）。  
- `inference_state`：scorer 端的 DuckDB 歷史保留長度（`history_window_min`）與 cold start 策略。  
- `guardrails`：  
  - 禁止在 `expression` 中出現 `SELECT`/`FROM`/`JOIN` 等關鍵字。  
  - 限定允許的聚合函數與 window 函數。  
  - 宣告 Track LLM 允許使用的欄位白名單。

### 2.2 Track LLM 區塊（bet-level 特徵）

重點欄位：

- `source_table`: `t_bet`  
- `guardrails.max_window_minutes`: 視窗長度上限（分）。  
- `guardrails.disallow_following: true`: 禁止使用未來視窗。  
- `candidates[]` 中每個特徵包含：
  - `feature_id`: 唯一識別（建議帶窗口與單位，如 `bets_cnt_w15m`）。  
  - `type`: `window` / `lag` / `transform` / `derived`。  
  - `dtype`: `int` / `float`。  
  - `expression`: 單純運算片段，例如 `COUNT(bet_id)`、`LAG(wager, 1)`、`EXTRACT(HOUR FROM payout_complete_dtm)`。  
  - `window_frame`（僅 `type=window` 需要）：例如 `RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW`。  
  - `depends_on`（僅 `type=derived` 需要）：依賴的其他 feature_id。  
  - `postprocess`: `fill`/`clip`/`safe_div_epsilon` 等穩定化設定。  
  - `reason_code_category`（可選）：對應 SSOT §12.1 中的 reason code 分類。

### 2.3 Track Human 區塊（狀態機與 Run-level）

- `function_name`: `features.py` 中對應的 Python 向量化函數名稱。  
- `input_columns`: 函數需要的原始欄位。  
- `output_columns`: 函數會產出的欄位（可超過 1 個，例如 `run_id`, `minutes_since_run_start`）。  
- `dtype`, `postprocess`, `reason_code_category` 同 Track LLM。

### 2.4 Track Profile 區塊（player_profile）

- `source_table`: `player_profile`  
- `join.join_key`: `canonical_id`  
- `join.snapshot_time_column`: `snapshot_dtm`  
- `join.pit_rule`: `asof_latest_snapshot_leq_event_time`（`snapshot_dtm <= bet_time` 最近一筆）  
- `join.on_missing_profile.strategy`: 缺 profile 時的策略（目前預設 `zero_fill`）。  
- `candidates[].source_column`: profile 表中的實際欄位名稱。  
- `feature_id`, `dtype`, `postprocess` 同上。

---

## 3. Feature Screening 與 Active List

1. 訓練流程讀取 candidates spec 產生完整 feature matrix。  
2. 執行兩階段 Feature Screening：  
   - 單變量 + 冗餘剔除（MI / variance / correlation / VIF 等）。  
   - 輕量 LightGBM importance/SHAP（只用 train set）。  
3. 將最終保留的 feature_id 寫入 `feature_list.json` 或 `features_active.yaml`。  
4. `trainer.py` 只用 active features 訓練模型；`scorer.py` 只計算 active features。

建議另在 model artifact metadata 中記錄：

- `candidates_spec_hash`: `features_candidates.yaml` 的 hash  
- `active_feature_hash`: `feature_list.json` 的 hash  

以便在 `/model_info` 或線上監控中追蹤模型與特徵定義版本。

---

## 4. 給未來 LLM 看的 Prompt 範本

> 下列 Prompt 範本假設你在對話中直接貼上 `features_candidates.template.yaml` 裡的 Track LLM 結構，並要求 LLM 產生多個新特徵。請務必保留「只寫 expression + window_frame，不寫 SELECT」這個約束。

**英文版本（推薦）**：

```text
You are helping me design bet-level features for a real-time walkaway prediction model in a casino baccarat setting.

Context:
- The raw data table is `t_bet`. Each row is a bet with at least the following fields:
  - bet_id (int, unique)
  - canonical_id (string, player identifier)
  - payout_complete_dtm (timestamp, bet event time)
  - wager (float)
  - payout_odds (float)
  - status (string, e.g. 'WIN', 'LOSE', 'PUSH')
  - is_back_bet (int)
  - position_idx (int)
- We already have a YAML schema for Track LLM (see below). Your job is to propose NEW candidate features that:
  - Only use columns from `t_bet`.
  - Are expressible as DuckDB window / lag / transform / derived expressions.
  - Are meaningful signals for "player is about to stop betting for at least 30 minutes within the next 15 minutes".

VERY IMPORTANT CONSTRAINTS:
- DO NOT write full SQL queries. For each feature you only provide:
  - `feature_id`
  - `type`: one of ["window", "lag", "transform", "derived"]
  - `dtype`: "int" or "float"
  - `expression`: an expression fragment WITHOUT SELECT/FROM/JOIN/UNION/WITH
  - `window_frame` (only when type == "window")
  - `depends_on` (only when type == "derived")
  - `description`, `rationale`
  - optional `postprocess.fill` and `postprocess.clip`
- For window features, window_frame MUST be "past and current row only" (PRECEDING + CURRENT ROW). Never use FOLLOWING.
- Use only allowed functions: COUNT, SUM, AVG, MIN, MAX, STDDEV_SAMP, LAG, basic arithmetic, CASE WHEN.
- Use only the allowed columns from `t_bet` listed above.

Output format:
- Return ONLY a YAML snippet that can be merged into the `track_llm.candidates` array of the spec.
- Do not modify other parts of the YAML.

Example of one valid candidate:

- feature_id: "bets_cnt_w15m"
  type: "window"
  dtype: "int"
  description: "Number of bets in the last 15 minutes"
  rationale: "Dropping betting frequency often precedes walkaway."
  expression: "COUNT(bet_id)"
  window_frame: "RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW"
  postprocess:
    fill:
      strategy: "zero"
    clip: { min: 0, max: 1000000 }
  reason_code_category: "BETTING_PACE_DROPPING"

Task:
- Propose 5–10 additional high-quality candidate features focused on:
  - recent betting pace changes
  - recent loss/win streak patterns that can be expressed as window stats
  - volatility of wager size across last few bets or minutes
- Return them as YAML items suitable to append under `track_llm.candidates`.
```

**繁中簡要補充**（可以視需要加在 Prompt 前後）：  

- 你可以在英文 Prompt 前加一句說明：「我們使用 DuckDB + YAML 來定義特徵，請嚴格遵守 schema，不要寫完整 SQL。」  
- 若要請 LLM 同時用中文解說 `description`/`rationale`，可以加一句：「`description` 與 `rationale` 字段請使用繁體中文，其餘欄位保持英文。」

---

## 5. 推薦使用流程（給未來的自己）

1. 從 `features_candidates.template.yaml` 複製為新檔案 `features_candidates.yaml`。  
2. 依照當前資料理解，手動調整：  
   - `track_llm.guardrails.max_window_minutes`  
   - `history_window_min` 與 buffer  
   - Track Profile / Track Human 的實際欄位與函數名稱。  
3. 把 Track LLM 的 schema 貼給 LLM，使用上面的 Prompt 請它產生一批候選特徵。  
4. 人工 review 這批 YAML snippet，貼回 `track_llm.candidates`。  
5. 跑一輪訓練 + Feature Screening，產生 `feature_list.json`。  
6. 將 `spec_hash` 和 `active_hash` 寫入新模型 artifact，部署後於 `/model_info` 中曝光。

