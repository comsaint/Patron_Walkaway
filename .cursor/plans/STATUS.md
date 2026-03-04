**Archive**: Past rounds are in [STATUS_archive.md](STATUS_archive.md). This file keeps the summary and the **latest rounds** only.

# STATUS — trainer.py Gap Analysis vs PLAN.md v10

**Date**: 2026-03-03
**Scope**: Compare existing `trainer/trainer.py` (1,171 lines) and `trainer/config.py` (90 lines) against `.cursor/plans/PLAN.md` v10 requirements.

---

## Summary

The existing trainer.py is a **Phase 1 refactor already in progress** — it has chunked processing, dual-model training, Optuna hyperparameter search, Track A/B features, identity mapping, and labels integration. However, several items are **out of date** compared to the latest PLAN.md v10 / SSOT v10 / DECISION_LOG updates. The changes are mostly **terminology + constant tweaks + sample weight logic**, not structural rewrites.

---

## Discrepancies Found

### P0 — Must Fix (Semantic / Logic)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 1 | `trainer.py` | L14, L747–763, L1119 | **Sample weight uses Visit (`canonical_id × gaming_day`), not Run** | `compute_sample_weights()` must use `run_id` from `compute_run_boundary()` instead of `canonical_id × gaming_day`. Docstring L14 and comment L1119 must change to "run-level". |
| 2 | `config.py` | L64 | **`SESSION_AVAIL_DELAY_MIN = 15`** | PLAN.md v10 / SSOT §4.2 says default **7** (with option for 15 as conservative). Change to `7`. |
| 3 | `config.py` | L74–77 | **G1 constants still active** (`G1_PRECISION_MIN`, `G1_ALERT_VOLUME_MIN_PER_HOUR`, `G1_FBETA`) | PLAN.md v10 / DEC-009/010: these are **deprecated / rollback only**. Mark as deprecated; do not remove (rollback path). |
| 4 | `trainer.py` | L80–82, L98–100 | **G1 constants imported** | Remove G1 imports. They are no longer used by trainer.py (threshold uses F1 only, DEC-009). |

### P1 — Should Fix (Missing Features per PLAN.md)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 5 | `trainer.py` | L954–1012 | **No `reason_code_map.json` in artifact bundle** | PLAN.md says artifacts include `reason_code_map.json` (feature → reason_code mapping). `save_artifact_bundle()` must generate it. |
| 6 | `trainer.py` | L286–294 | **Session ClickHouse query missing FND-04 turnover filter** | PLAN.md Step 1 / SSOT §5: sessions must also satisfy `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`. Currently only filters `is_deleted=0 AND is_canceled=0 AND is_manual=0`. |
| 7 | `trainer.py` | N/A | **No `player_profile_daily` PIT/as-of join** | PLAN.md Step 4/5: Rated bets should be enriched with `player_profile_daily` columns via PIT/as-of join (`snapshot_dtm <= bet_time`). Not implemented yet. Blocked on `doc/player_profile_daily_spec.md` and table existence. |

### P2 — Terminology / Comments (Cosmetic but important for consistency)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 8 | `config.py` | L54 | Says "SSOT v9" | Change to "SSOT v10". |
| 9 | `config.py` | L69 | Comment says "Gaming day / **visit** dedup (G4)" | Change to "Gaming day / **run** dedup (G4)". |
| 10 | `trainer.py` | L5 | Docstring says "sample_weight = 1 / N_visit" | Change to "sample_weight = 1 / N_run". |
| 11 | `trainer.py` | L747 | Section header: "Visit-level sample weights (SSOT §9.3)" | Change to "Run-level sample weights". |
| 12 | `trainer.py` | L751 | Docstring: "Return sample_weight = 1 / N_visit" | Change to "Return sample_weight = 1 / N_run". |
| 13 | `trainer.py` | L753 | Docstring: "N_visit = ... per (canonical_id, gaming_day)" | Change to "N_run = number of bets in the same run (same canonical_id, same run_id from compute_run_boundary)". |
| 14 | `trainer.py` | L757 | Warning message: "visit weights" | Change to "run weights". |
| 15 | `trainer.py` | L1119 | Comment: "Optuna + visit-level sample_weight" | Change to "Optuna + run-level sample_weight". |

### DECISION_LOG Conflict (Must Resolve)

| # | File | Issue | Required Action |
|---|------|-------|-----------------|
| 16 | `DECISION_LOG.md` | **RESOLVED** | DEC-013 has been updated to reflect the latest agreed decision: sample weighting changed from visit-level to **run-level** (not removed). |

---

## Items Already Correct (No Change Needed)

- **Dual-model architecture** (Rated / Non-rated): Implemented correctly.
- **Optuna TPE hyperparameter search**: Implemented, optimises PR-AUC on validation.
- **F1 threshold selection** (DEC-009): L854 correctly maximises F1, no G1 constraint.
- **Track B Phase 1**: Only `loss_streak` + `run_boundary` — no `table_hc`. Correct.
- **Track A DFS**: Two-stage flow (explore → save_features → calculate_feature_matrix). Correct.
- **DQ guardrails**: `t_bet` uses `payout_complete_dtm IS NOT NULL`, `wager > 0`, `player_id != PLACEHOLDER`. `t_session` uses NO `FINAL`, FND-01 dedup. All correct.
- **C1 extended pull**: Labels use `extended_end` for gap detection, but training rows are filtered to `[window_start, window_end)`. Correct.
- **H1 censored bets**: Dropped at L676. Correct.
- **TRN-07 cache validation**: Present at L618–627. Correct.
- **Atomic artifact bundle**: model_version + dual .pkl + feature_list.json. Correct (except missing reason_code_map.json, see P1-5).
- **Legacy backward compat**: `walkaway_model.pkl` still written. Correct.
- **Local Parquet dev path**: Fully implemented with same DQ. Correct.

---

## Recommended Edit Order

1. ~~**config.py** — P0-2 (SESSION_AVAIL_DELAY_MIN=7), P0-3 (G1 deprecated), P2-8/9 (terminology).~~ **DONE 2026-03-03**
2. ~~**trainer.py** — P0-1 (run-level sample weight logic), P0-4 (remove G1 imports), P1-6 (FND-04 session filter), P1-5 (reason_code_map.json), P2-* (all terminology).~~ **DONE 2026-03-03**
3. ~~**DECISION_LOG.md** — Resolve DEC-013 conflict (run-level vs removed).~~ **DONE 2026-03-03**
4. **trainer.py** — P1-7 (player_profile_daily PIT join) — deferred until spec + table ready.

---

## Estimated Effort

- **P0 + P1 + P2 edits** (items 1–15): ~30 min of targeted edits. No structural/architectural change.
- **DEC-013 conflict resolution** (item 16): 5 min decision + 5 min edit.
- **player_profile_daily** (item 7): Blocked on external dependency; skip for now.

---

## Technical Review & Edge Cases (trainer.py Refactor)

Date: 2026-03-03

### 1. P0-1: Run-level Sample Weight Logic (`1 / N_run`)
- **Potential Bug / Edge Case**: 
  1. 如果某些 bet 沒有對應的 `run_id` (例如 Track B 特徵生成失敗)，直接計算 `value_counts()` 會漏掉資料。
  2. 若 `run_id` 僅在 player 內部遞增 (0, 1, 2...) 而非全域唯一，不同玩家的 `run_id=1` 會被算在一起！必須將 `canonical_id` 與 `run_id` 組合為 key。
  3. **資料洩漏 (Data Leakage) 風險**：`N_run` 若是在切分 train/valid 之前以全表計算，會洩漏未來資訊。必須確保 `compute_sample_weights` 僅作用於已經切分好的 `train_df`，且計算的 `N_run` 就是該 run 在 **train set 內的樣本數**。
- **具體修改建議**：
  在 `compute_sample_weights` 內，加入欄位檢查並使用複合鍵：
  ```python
  if "run_id" not in df.columns or "canonical_id" not in df.columns:
      logger.warning("Missing canonical_id or run_id; returning weight 1.0")
      return pd.Series(1.0, index=df.index)
  run_key = df["canonical_id"].astype(str) + "_" + df["run_id"].astype(str)
  n_run = run_key.map(run_key.value_counts())
  return (1.0 / n_run).fillna(1.0)
  ```
- **希望新增的測試**：`test_trainer_compute_sample_weights_run_logic`：傳入一個 DataFrame (兩位玩家，各有多個 runs)，驗證產出的權重為 `1/該玩家該run的總數`。

### 2. P1-6: FND-04 Session Filter (`turnover > 0`)
- **效能/邊界條件問題**：
  在 `load_clickhouse_data` 中，SQL 會加上 `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`。但是 `turnover` 欄位目前**並不在** `_SESSION_SELECT_COLS` 清單中。如果 `load_local_parquet` 依賴 parquet 檔案，而原本的匯出沒包含 `turnover`，就會發生 KeyError。
- **具體修改建議**：
  1. 將 `turnover` 補入 `_SESSION_SELECT_COLS`。
  2. 在 `load_local_parquet` 中加上防禦性讀取：`sess.get("turnover", pd.Series(0))`。
- **希望新增的測試**：`test_apply_dq_session_turnover`：給定 mock sessions (有/無 turnover)，確保 turnover=0 且 num_games_with_wager=0 的 session 正確被濾除。

### 3. P1-5: `reason_code_map.json`
- **安全性/錯誤處理問題**：
  `save_artifact_bundle` 需要產出這份 JSON 供線上 Scorer 查詢。但 Track A (Featuretools) 會動態生出不可預測的特徵名稱 (例如 `SUM(t_session.turnover)`)，我們無法人工窮舉所有對應的 Reason Code。如果遺漏，線上預測時會出錯或顯示 UNKNOWN。
- **具體修改建議**：
  在 `save_artifact_bundle` 內實作自動生成邏輯：先定義 Track B 與 Legacy 特徵的靜態字典 (如 `"loss_streak": "LOSS_STREAK"`), 對於未知的特徵，直接使用特徵全名大寫或是給定 fallback code (`RSN_TRACK_A`)，確保 json 內包含 `feature_cols` 中的「所有」特徵。
- **希望新增的測試**：`test_save_artifact_bundle_reason_codes`：送入包含動態名稱的 `feature_cols`，檢查輸出的 json 涵蓋了所有欄位。

---

## Tests added/updated (Review risks → minimal repro)

Date: 2026-03-03

### Updated
- `tests/test_trainer.py`
  - **Run-level sample_weight spec**: updated from `1/N_visit` to `1/N_run` (key = `canonical_id` + `run_id`).

### Added (in `tests/test_trainer.py`)
- `TestReviewRiskGuards.test_load_clickhouse_data_session_query_has_fnd04_turnover_guard`
  - Enforces that `load_clickhouse_data()` session query logic includes the **FND-04** filter:
    `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`.
- `TestReviewRiskGuards.test_save_artifact_bundle_writes_reason_code_map_json`
  - Enforces that `save_artifact_bundle()` writes `reason_code_map.json`.

### How to run
- Run only the trainer-related tests:
  - `python -m unittest tests.test_trainer -v`
- Run all unit tests:
  - `python -m unittest -v`

### Notes
- ~~These tests are expected to **fail right now** until `trainer/trainer.py` is updated.~~ **All 3 tests now pass (2026-03-03).**

---

## Technical Review Round 5 — Post-Round-4 Cross-File Audit (2026-03-03)

深度審查 `trainer/trainer.py`（含 Round 3/4 變更）+ `trainer/backtester.py`。  
依嚴重性排序。

---



### R501 — `run_pipeline()` 中 `effective_start` / `effective_end` 仍為 tz-aware，傳給 `load_local_parquet` / `apply_dq` / `load_player_profile_daily`

**嚴重度**：P1（潛在，目前被 `_filter_ts()` 和 `apply_dq` 內部守衛擋住，但脆弱）  
**位置**：`trainer/trainer.py:1815–1816, 1856–1865, 1939–1940`  
**問題**：`effective_start` 和 `effective_end` 直接取自 `chunks[0]["window_start"]`（tz-aware），然後傳給：
- `load_local_parquet(effective_start, effective_end + timedelta(days=1))` — `_filter_ts()` 能處理，目前安全
- `apply_dq(pd.DataFrame(...), sessions_all, effective_start, effective_end + timedelta(days=1))` — `apply_dq` 內部的 `_lo`/`_hi` 守衛能 strip，目前安全
- `load_player_profile_daily(effective_start, effective_end, ...)` — 裡面有 `_naive()` helper，目前安全

這些地方目前不會崩是因為每個下游函數各自有防呆。但若未來有人移除其中任一防呆（例如清理「多餘」的 `.replace(tzinfo=None)`），就會爆。

**修改建議**：在 `run_pipeline()` 中 `effective_start` / `effective_end` 賦值後，立即 strip tz（與 `process_chunk()` 入口同一邏輯）：

```python
effective_start = effective_start.replace(tzinfo=None) if effective_start.tzinfo else effective_start
effective_end   = effective_end.replace(tzinfo=None)   if effective_end.tzinfo   else effective_end
```

**建議測試**：可與 R500 共用一個整合測試。

---

### R502 — PLAN.md 步驟 4（防呆 assertion）未實作

**嚴重度**：P2（不是 bug，但 PLAN 明確標為「推薦」）  
**位置**：`trainer/trainer.py` `apply_dq()` 回傳前 / `process_chunk()` strip 後  
**問題**：PLAN.md DEC-018 §4 明確建議在兩處加 `assert`，Round 46 STATUS 也提到此步驟，但實際未實作。缺少 assertion 意味著若未來有人意外移除 R23 strip 或 DEC-018 strip，不會立即被偵測到。

**修改建議**：在 `apply_dq()` 回傳 `bets` 前（大約 `return bets, sessions` 之前）加：

```python
if not bets.empty:
    assert bets["payout_complete_dtm"].dt.tz is None, \
        "R23 violation: payout_complete_dtm must be tz-naive after DQ"
```

在 `process_chunk()` DEC-018 strip 後加（三個邊界都要檢查）：

```python
for _name, _val in [("window_start", window_start), ("window_end", window_end), ("extended_end", extended_end)]:
    assert getattr(_val, "tzinfo", None) is None, \
        f"DEC-018: {_name} must be tz-naive inside process_chunk (got {_val})"
```

**建議測試**：`test_apply_dq_asserts_tz_naive` — 確認 `apply_dq` 回傳後 `payout_complete_dtm.dt.tz is None`。

---

### R503 — `_chunk_parquet_path(chunk)` 用原始 tz-aware chunk dict 時，isoformat 格式會因 tz 改變

**嚴重度**：P2（Cache invalidation — 不是 crash，但會產出不同的 cache key）  
**位置**：`trainer/trainer.py:1146–1147`  
**問題**：`_chunk_cache_key()` 用 `chunk["window_start"].isoformat()`，tz-aware 的 isoformat 會是 `2026-02-06T00:00:00+08:00`，而若未來 chunk 被 strip 後再算 key，會變成 `2026-02-06T00:00:00`。這意味著**同一份資料在 DEC-018 前後的 cache key 不一致**，導致一次性的全量 cache miss。

**影響**：只會在第一次跑時 recompute 所有 chunk，不影響正確性。但如果使用者已有大量 cache，會浪費時間重算。

**修改建議**：目前不需改。`_chunk_parquet_path` 和 `_chunk_cache_key` 正確地使用原始 `chunk` dict（保持 tz-aware），所以 key 格式不變。**但需在 STATUS 中明確記錄此設計意圖**：cache helper 用原始 chunk dict（tz-aware），process_chunk 內部用 strip 後的值；兩者不要混用。

**建議測試**：無（目前行為正確）。

---

### R504 — `run_pipeline()` concat 後的 tz strip 是冗餘的（DEC-018 後應不再需要）

**嚴重度**：P3（Code smell / 死碼）  
**位置**：`trainer/trainer.py:1987–1988`

```python
if _payout_ts.dt.tz is not None:
    _payout_ts = _payout_ts.dt.tz_localize(None)
```

**問題**：DEC-018 步驟 2 在 `apply_dq()` 中已保證 `payout_complete_dtm` 是 tz-naive `datetime64[ns]`。所有 chunk Parquet 都是從 `process_chunk()` 寫出的，其中 `labeled` 的 `payout_complete_dtm` 已經是 tz-naive。所以 `_payout_ts.dt.tz is not None` 永遠為 `False`，這兩行是死碼。

**修改建議**：保留作為防呆（如果從外部 Parquet 讀回時 tz 不一致），但加註解說明「DEC-018 後此分支理論上不會觸發」。或直接移除。

**建議測試**：無。

---

### R505 — `features.py` 中 `compute_loss_streak` / `compute_run_boundary` / `compute_table_hc` 的 docstring 仍標示接受 `datetime`，未提及必須 tz-naive

**嚴重度**：P3（文件不一致）  
**位置**：`trainer/features.py` 多處 docstring  
**問題**：`cutoff_time : datetime | None` 的 docstring 未說明 DEC-018 後此參數必須為 tz-naive，若有新開發者傳入 tz-aware 的 `datetime` 會靜默出錯（pandas 比較可能 raise 或靜默返回全 False）。

**修改建議**：在 `cutoff_time` 的 docstring 加一句：`Must be tz-naive (HK local time); see DEC-018.`

**建議測試**：`test_compute_loss_streak_tz_aware_cutoff_raises` — 傳入 tz-aware cutoff，確認 raise TypeError（驗證 DEC-018 的「不再容忍 tz-aware」契約）。

---

### 修改優先順序

| 風險 | 優先 | 難度 |
|------|------|------|
| R500 | P0 | 3 行 |
| R501 | P1 | 2 行 |
| R502 | P2 | 6 行 |
| R503 | P2 | 0 行（記錄設計意圖即可） |
| R504 | P3 | 1 行註解或 2 行移除 |
| R505 | P3 | 3 行 docstring |

### 建議新增的測試

| 測試名稱 | 涵蓋 | 檔案 |
|----------|------|------|
| `test_backtester_tz_aware_input_no_crash` | R500 | `tests/test_backtester_review_risks_round18.py` 或新建 |
| `test_apply_dq_output_tz_naive_ns` | R502, DEC-018 步驟 2 | `tests/test_review_risks_round170.py`（新） |
| `test_process_chunk_strips_tz` | R502 | 同上 |
| `test_compute_loss_streak_tz_aware_cutoff_raises` | R505 | `tests/test_features.py` 或新建 |

### 下一步

1. **P0 / P1 先修**：R500（backtester strip）和 R501（run_pipeline strip）應該在下一輪立即修掉，否則 backtester 在 DEC-018 後必崩。
2. **P2 跟進**：R502（assertion）在 P0/P1 修完後加入。
3. **跑 pipeline 驗收**：全部修完後再跑一次 `python -m trainer.trainer --fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 500`。

---

## Round 48（2026-03-04）— R500-R505 最小可重現測試（tests-only）

### 修改目標

依 Reviewer（Round 47）提出的 R500-R505 風險，新增「最小可重現測試 / 結構 guardrail」，**不修改任何 production code**。

### 新增檔案

- `tests/test_review_risks_round170.py`

### 測試設計

| 風險 | 測試名稱 | 類型 | 目前狀態 |
|------|----------|------|----------|
| R500 | `test_backtest_tz_aware_window_should_not_raise_typeerror` | runtime 可重現（用 patch 建最小資料） | `expectedFailure` |
| R501 | `test_run_pipeline_should_strip_tz_on_effective_window` | source 結構 guardrail | `expectedFailure` |
| R502 | `test_apply_dq_should_assert_tz_naive_output` / `test_process_chunk_should_assert_tz_naive_boundaries` | source 結構 guardrail | `expectedFailure` |
| R503 | `test_chunk_cache_key_uses_original_chunk_isoformat` | source 設計意圖 guardrail | `pass` |
| R504 | `test_concat_split_keeps_defensive_tz_strip` | source 防禦性 guardrail | `pass` |
| R505 | `test_track_b_docstrings_should_mention_tz_naive_cutoff` | doc contract guardrail | `expectedFailure` |

> 說明：對於「已確認但尚未修復」的風險，使用 `@unittest.expectedFailure`，可在 tests-only 階段保留風險可見性，同時不阻塞整體測試流程。

### 執行結果

```bash
c:/Users/longp/Patron_Walkaway/walkaway/Scripts/python.exe -m unittest tests.test_review_risks_round170 -v
```

結果：

```text
Ran 7 tests in 0.019s
OK (expected failures=5)
```

### 執行方式（目前環境）

目前環境沒有 `pytest`（`No module named pytest`），所以本輪使用 `unittest`：

```bash
c:/Users/longp/Patron_Walkaway/walkaway/Scripts/python.exe -m unittest tests.test_review_risks_round170 -v
```

若後續安裝 `pytest`，可改為：

```bash
python -m pytest tests/test_review_risks_round170.py -q
```

### 下一步建議

1. 在下一輪 production 修復 R500 / R501 / R502 / R505 後，把對應 `expectedFailure` 逐一移除並轉為正常綠燈測試。  
2. 保留 R503 / R504 這類結構 guardrail，避免後續 refactor 破壞 cache key 與 split 路徑的防禦性行為。  
3. 若要將本檔納入 CI，建議先統一測試 runner（`pytest` 或 `unittest`）與環境依賴。

---

## Round 49 (2026-03-04) — R500–R505 Production 修復：所有測試全綠


### 目標

接續 Round 48 測試-only 輪次，本輪把 R500–R505 四個風險點的 production code 全部修好，並移除測試中的 `@unittest.expectedFailure` 標記，使 7 條測試由 "OK (expected failures=5)" → "OK (全綠)"。

### 修改檔案

| 檔案 | 風險 | 修改內容 |
|------|------|---------|
| `trainer/backtester.py` | R500 | 在 `backtest()` 計算完 `extended_end` 之後，加 DEC-018 strip：`window_start/window_end/extended_end = *.replace(tzinfo=None) if *.tzinfo`；將 `ws_naive/we_naive` 改為直接引用已 strip 的變數（不再重複 strip）。 |
| `trainer/trainer.py` | R501 | 在 `run_pipeline()` 的 `effective_start`/`effective_end` 賦值後，加 DEC-018 strip：兩行 `= *.replace(tzinfo=None) if *.tzinfo else *`。 |
| `trainer/trainer.py` | R502-1 | 在 `apply_dq()` 的 `return bets, sessions` 之前，加 assertion：`assert bets["payout_complete_dtm"].dt.tz is None, "R23 violation: payout_complete_dtm must be tz-naive after DQ"`。 |
| `trainer/trainer.py` | R502-2 | 在 `process_chunk()` 的 DEC-018 strip 之後，加 assertion loop：`assert getattr(_bval, "tzinfo", None) is None, "DEC-018: {_bname} must be tz-naive inside process_chunk"`。 |
| `trainer/features.py` | R505 | 在 `compute_loss_streak`、`compute_run_boundary`、`compute_table_hc` 的 `cutoff_time` 參數說明中，加一行：`"Must be **tz-naive** (DEC-018 contract)"`。 |
| `tests/test_review_risks_round170.py` | 全部 | 移除 5 個 `@unittest.expectedFailure` 標記（R500/R501/R502×2/R505），原測試邏輯不變。 |

### 設計意圖

- **R500**：`backtester.backtest()` 是繞過 `process_chunk` 的獨立路徑，本身未做 DEC-018 strip。修後 `compute_labels`、`add_track_b_features`、label filter 全部收到 tz-naive 邊界，與 trainer 路徑保持一致。
- **R501**：`run_pipeline()` 的 `effective_start`/`effective_end` 用於 `ensure_player_profile_daily_ready`、`load_player_profile_daily`、`apply_dq`（canonical map path）等下游呼叫；若從 tz-aware chunks 繼承邊界，這些呼叫也可能炸 TypeError。
- **R502**：assert 讓 `apply_dq` / `process_chunk` 的 DEC-018 合約在 runtime 可見，未來若有人不小心改壞 strip 邏輯，立即報錯而非沉默出錯。
- **R505**：docstring 記錄 tz-naive 合約，讓 code reviewer 和 AI assistant 都能從 API 文件得到提示。

### 測試結果

```bash
c:/Users/longp/Patron_Walkaway/walkaway/Scripts/python.exe -m unittest tests.test_review_risks_round170 -v
```

```text
test_backtest_tz_aware_window_should_not_raise_typeerror ... ok
test_run_pipeline_should_strip_tz_on_effective_window ... ok
test_apply_dq_should_assert_tz_naive_output ... ok
test_process_chunk_should_assert_tz_naive_boundaries ... ok
test_chunk_cache_key_uses_original_chunk_isoformat ... ok
test_concat_split_keeps_defensive_tz_strip ... ok
test_track_b_docstrings_should_mention_tz_naive_cutoff ... ok

Ran 7 tests in 0.062s
OK
```

**全 7 條綠燈，無 expected failures。**

### 下一步建議

1. 執行完整的 pipeline smoke test（`--fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 500`）確認真實資料路徑也無 tz 錯誤。
2. 若 CI 環境已有 `pytest`，可加入 `pytest tests/test_review_risks_round170.py -q` 至 pre-commit 或 CI workflow。
3. R503/R504（cache key 與 split defensive strip）guardrail 維持現狀，未來 refactor 前必須先讓這兩條測試通過。

---

## Round 50 (2026-03-04) — DEC-018 確認完成 + DEC-019 月結 Profile Snapshot 實作

### 目標

依 PLAN.md 第 1–2 步：
1. **DEC-018（tz 統一）**：確認核心修復已到位；不做額外破壞性刪除（保留 features.py 防禦性補丁）。
2. **DEC-019（月結 Profile ETL）**：實作「每月最後一天」profile snapshot 排程，讓 full-run profile ETL 從約 4–6 h 降至約 12 min。

### DEC-018 現況確認（無新改動）

| 位置 | 修復內容 | 狀態 |
|------|---------|------|
| `trainer.py` `process_chunk()` L1217–1219 | Strip `window_start/window_end/extended_end` tz | ✅ 已完成（Round 49） |
| `trainer.py` `apply_dq()` L1009, L1012 | tz strip + `.astype("datetime64[ns]")` | ✅ 已完成（Round 49） |
| `trainer.py` `process_chunk()` L1221–1223 | 防呆 assertion | ✅ 已完成（Round 49） |
| `features.py` `join_player_profile_daily()` | 防禦性 tz 補丁（保留，defense-in-depth） | ✅ 維持現狀 |
| Round 170 tests（7 條） | 全綠 | ✅ 已完成（Round 49） |

### DEC-019 修改檔案

| 檔案 | 修改內容 |
|------|---------|
| `trainer/etl_player_profile.py` | `backfill()` 新增 `snapshot_dates: Optional[List[date]] = None` 參數；當提供時以此列表取代 day-by-day 迴圈；`preloaded_sessions` trigger 條件加入 `snapshot_dates is not None` |
| `trainer/trainer.py` | 新增 `_month_end_dates(start_date, end_date) -> List[date]` helper（用 `calendar.monthrange`）；`ensure_player_profile_daily_ready()` 新增 `use_month_end_snapshots: bool = True` 參數；計算 `_snap_dates` 並傳入 `_etl_backfill(snapshot_dates=_snap_dates)`；`use_inprocess` 條件加入 `_snap_dates is not None`；coverage check 用 `_effective_interval = 31`（月結）取代固定 `snapshot_interval_days`；`run_pipeline` 的呼叫加 `use_month_end_snapshots=getattr(args, "month_end_snapshots", True)`；新增 `--no-month-end-snapshots` CLI flag（`store_false`，dest=`month_end_snapshots`） |

### 行為變化

| 情境 | 原行為 | 新行為 |
|------|--------|--------|
| Full run（無 `--fast-mode`） | 每日 365 個 snapshot，ETL ~4–6 h | 每月最後一天 12 個 snapshot，ETL ~12 min |
| Fast-mode（`--fast-mode`） | `snapshot_interval_days=7` | **不受影響**（`use_month_end_snapshots` 在 fast_mode=True 時自動無效） |
| 加 `--no-month-end-snapshots` | N/A | 恢復 daily/interval 行為 |
| PIT join | `snapshot_dtm <= bet_time` | **不變** |
| profile 覆蓋率檢查 | `snapshot_interval_days > 1` 才放寬 | 月結模式（`_effective_interval=31`）也放寬 |

### 如何手動驗證

1. **月結日期是否正確**：
   ```python
   from trainer.trainer import _month_end_dates
   from datetime import date
   print(_month_end_dates(date(2025, 11, 15), date(2026, 2, 28)))
   # 預期: [date(2025, 11, 30), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28)]
   ```

2. **backfill snapshot_dates 路徑**（dry-run 只看 log，不須真實資料）：
   ```python
   from trainer.etl_player_profile import backfill
   from datetime import date
   # Should log "backfill (DEC-019 snapshot_dates): 2 dates in [2026-01-01, 2026-02-28]"
   # then attempt to build Jan 31 + Feb 28 snapshots
   ```

3. **Full run（不加 --fast-mode）**：
   ```bash
   python -m trainer.trainer --use-local-parquet --recent-chunks 1
   ```
   觀察 log 中 `ensure_player_profile_daily_ready` 段落應出現：
   - `backfill (DEC-019 snapshot_dates): N dates in [...]`
   - `player_profile_daily coverage acceptable (month-end).`
   - `ensure_player_profile_daily_ready: < 60s`（而非原來的數小時）

4. **Opt-out 驗證**：
   ```bash
   python -m trainer.trainer --use-local-parquet --recent-chunks 1 --no-month-end-snapshots
   ```
   log 應回到 `backfill: using explicit snapshot_dates` 路徑消失，改為逐日迴圈。

5. **Fast-mode 不受影響**：
   ```bash
   python -m trainer.trainer --fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 500
   ```
   行為應與 Round 49 後相同（`interval=7` 路徑）。

### 下一步建議

1. 執行第 3 步的手動驗證（full run `--use-local-parquet --recent-chunks 1`），確認月結 profile ETL 約 12 min 完成。
2. 執行第 5 步 smoke test（fast-mode + sample-rated 500），確認 DEC-018 tz 修復在真實資料路徑有效（terminal 之前的 crash 可能是修復前的記錄）。
3. 如 smoke test 通過，考慮新增 `test_month_end_dates_correctness` 與 `test_backfill_snapshot_dates_path` 單元測試。

---

## Round 50 Review — DEC-019 月結 Snapshot 實作 Code Review

**日期**：2026-03-04  
**範圍**：Round 50 新增/修改的程式碼：`trainer/trainer.py`（`_month_end_dates`、`ensure_player_profile_daily_ready`、`run_pipeline`、CLI）、`trainer/etl_player_profile.py`（`backfill`）

### R600 — `_month_end_dates` 空列表：missing_range 跨月但月結日不在範圍內

**嚴重性**：Bug（可能導致 profile ETL 不建任何 snapshot）  
**位置**：`trainer.py` `_month_end_dates()` + `ensure_player_profile_daily_ready()` L864

**問題**：  
`missing_ranges` 是從現有 profile 覆蓋的「缺口」計算的。例如 profile 已有到 1/15，required_end = 2/13 → missing_range = (1/16, 2/13)。此時 `_month_end_dates(date(2026,1,16), date(2026,2,13))` 回傳 `[date(2026,1,31)]`（1/31 在範圍內，2/28 不在），只會建一個 1/31 snapshot。**但 2 月的注單（2/1–2/13）就只能用 1/31 的 snapshot，沒有 2 月的 snapshot 了。**

這本身不是 bug（PIT join 會 fallback 到 1/31），但使用者預期「覆蓋 2/13」時，coverage check `after_end < required_end - 31` → `1/31 < 2/13 - 31 = 1/13` 為 False → 判定為 acceptable。所以 **coverage check 不會警告**，行為正確但不直觀。

更嚴重的情境：若 required_range = (2/1, 2/13)，`_month_end_dates` 回傳空列表 `[]`，`dates_to_process` 為空，**backfill 不建任何 snapshot**，但 log 仍顯示 "0 dates in [...]"，coverage check 可能仍判定 acceptable（因為已有先前的 1/31 snapshot）。

**具體修改建議**：  
在 `_snap_dates` 為空列表時 log 一個 warning，且 fallback 回 interval-based 行為：
```python
_snap_dates = _month_end_dates(miss_start, miss_end) if (...) else None
if _snap_dates is not None and len(_snap_dates) == 0:
    logger.warning(
        "DEC-019: no month-end dates in missing range %s -> %s; "
        "falling back to interval-based backfill",
        miss_start, miss_end,
    )
    _snap_dates = None
```

**希望新增的測試**：  
`test_month_end_dates_empty_when_range_within_single_month`：  
`_month_end_dates(date(2026,2,1), date(2026,2,13))` → 預期 `[]`；確認行為清晰。  
`test_ensure_profile_fallback_when_no_month_end_in_range`：  
模擬 missing_range = (2/1, 2/13)，確認不會靜默跳過 backfill。

---

### R601 — Schema hash 不含 snapshot 排程模式 → 月結/每日快取互相覆蓋

**嚴重性**：Bug（快取汙染）  
**位置**：`trainer.py` L758–768（schema hash 計算）+ `etl_player_profile.py` L841–848

**問題**：  
Schema hash 目前包含 `_pop_tag`（whitelist 人數或 "full"）和 `_horizon_tag`（`_mlb=365`），但**不包含 snapshot 排程模式**（月結 vs 每日）。這代表：

1. 先跑 full run（月結，12 個 snapshot）→ profile parquet 包含 12 個月結日
2. 再跑 `--no-month-end-snapshots`（每日 365 個 snapshot）→ schema hash 相同 → **不刪除快取**
3. 第二次 run 看到 profile 覆蓋「不足」→ append 日期到既有 parquet

這本身**不會產生錯誤資料**（PIT join 有更多選擇只會更準確），但第二次 run 不會從頭建 365 天的 daily snapshot，而是只補「缺口」。**如果使用者反復切換月結/每日模式，profile parquet 內容會變成混合的零散日期，不太直觀。**

**具體修改建議**：  
在 `_pop_tag` 旁加 snapshot 排程 tag：
```python
_sched_tag = "_month_end" if (use_month_end_snapshots and not fast_mode) else "_daily"
current_hash = hashlib.md5((current_hash + _pop_tag + _horizon_tag + _sched_tag).encode()).hexdigest()
```
同樣在 `etl_player_profile.py` 的 `_persist_local_parquet` 裡的 sidecar hash 計算也要加入。但此處有困難：`build_player_profile_daily` 不知道自己是被月結還是每日呼叫的。

**務實替代方案**：暫時不改 schema hash，但在 STATUS.md 記錄此為 known limitation。

**希望新增的測試**：  
`test_schema_hash_differs_between_month_end_and_daily`（若實作）

---

### R602 — Normal-mode full-population preload 觸發 OOM 風險

**嚴重性**：效能/OOM  
**位置**：`etl_player_profile.py` L1098–1101

**問題**：  
在 normal mode + 月結（DEC-019）、無 `--sample-rated`、無 `--fast-mode` 時：
- `canonical_map` 由 trainer 傳入（非 None）
- `snapshot_dates` 非 None

兩者都會觸發 `preload_sessions=True` 路徑 → `_preload_sessions_local()` 載入全部 69M 列 session（約 4–6 GB RAM）。在 **8 GB RAM** 機器上，這很可能 OOM。

之前 preload 只在 fast-mode / whitelist 才觸發（R112），但 DEC-019 新加的 `snapshot_dates is not None` 條件讓 normal-mode 也會觸發。

**具體修改建議**：  
月結模式只有 12 個 snapshot 日，每天用 `_load_sessions_local` 的 PyArrow pushdown 讀取也才 12 次，完全可以接受。不需要 preload。改條件：
```python
if preload_sessions and use_local_parquet and (
    snapshot_interval_days > 1 or canonical_id_whitelist is not None
):
```
也就是**移除 `snapshot_dates is not None` 條件**。月結模式下，preloaded_sessions = None，每個 snapshot 日走 `_load_sessions_local` pushdown 讀取。

或者讓月結也 preload 但在前面加 `len(snapshot_dates) > X` 的門檻判斷（如果 snapshot 次數多就 preload），但目前月結最多 12 次，不值得 preload。

**希望新增的測試**：  
`test_backfill_month_end_does_not_preload_when_no_whitelist`：確認 `snapshot_dates` 不觸發 preload。

---

### R603 — Log 訊息仍寫 "for fast-mode (interval=N days)"

**嚴重性**：Cosmetic（log 誤導）  
**位置**：`etl_player_profile.py` L1104–1106

**問題**：  
backfill 裡 preload 成功後的 log 寫：
```
"backfill: session parquet preloaded once (%d rows) for fast-mode (interval=%d days)"
```
但月結模式不是 fast-mode，且 `snapshot_interval_days` 在月結模式下會是 1（沒有意義）。

**具體修改建議**：  
```python
_mode_desc = (
    f"DEC-019 month-end ({len(snapshot_dates)} dates)" if snapshot_dates is not None
    else f"fast-mode (interval={snapshot_interval_days} days)"
)
logger.info(
    "backfill: session parquet preloaded once (%d rows) for %s",
    len(preloaded_sessions), _mode_desc,
)
```

**希望新增的測試**：無（cosmetic）

---

### R604 — `_month_end_dates` 的 `import calendar` 放在函式內部

**嚴重性**：效能（微小）/ 風格  
**位置**：`trainer.py` L675

**問題**：  
`import calendar as _cal` 在每次呼叫 `_month_end_dates` 時都會執行。雖然 Python 的 module cache 讓重複 import 幾乎免費（只是一次 dict lookup），但風格上不一致——`trainer.py` 其他 import 都在檔案頂部。

**具體修改建議**：  
把 `import calendar as _cal` 移到檔案頂部的 import 區段。或者，鑒於 `calendar` 是標準庫且一定存在，放函式內也可接受——不是必修項。

**希望新增的測試**：無

---

### R605 — `--sample-rated` + 月結模式的交互未明確定義

**嚴重性**：邊界條件  
**位置**：`trainer.py` `ensure_player_profile_daily_ready` L864

**問題**：  
當同時使用 `--sample-rated 500`（非 fast-mode）時：
- `rated_whitelist` 非 None → `canonical_id_whitelist` 非 None
- `use_month_end_snapshots = True`，`fast_mode = False`
- `_snap_dates` 會是月結日期列表

但同時 `snapshot_interval_days = 1`（non-fast），`use_inprocess = True`（因為 whitelist 非 None），backfill 收到 `snapshot_dates=月結列表` + `canonical_id_whitelist=500 IDs`。

**行為正確**——月結排程 + 500 人 whitelist 會正確地只在月結日建 500 人的 snapshot。但 **schema hash 中的 `_pop_tag=_whitelist=500`** 會讓這個快取與「月結 + full population」不同，不會互相汙染。✅ 無需修改。

**希望新增的測試**：  
`test_sample_rated_with_month_end_snapshots_produces_expected_dates`

---

### 問題匯總與優先級

| # | 問題 | 嚴重性 | 需要改 code |
|---|------|--------|-------------|
| R600 | `_snap_dates` 可能為空列表，靜默跳過 backfill | Bug | 是 |
| R601 | Schema hash 不含排程模式 | Known limitation | 暫不改（記錄即可） |
| R602 | Normal-mode 月結觸發 preload → 低 RAM OOM | OOM 風險 | 是 |
| R603 | Preload log 寫 "fast-mode" 但實際非 fast-mode | Cosmetic | 建議改 |
| R604 | `import calendar` 在函式內 | 微小 / 風格 | 可選 |
| R605 | `--sample-rated` + 月結交互 | 邊界條件（已確認正確） | 否 |

### 建議的修復優先序

1. **R600**（空列表 fallback）— 避免靜默跳過 backfill
2. **R602**（移除 preload 的 `snapshot_dates` trigger）— 避免 OOM
3. **R603**（log 修正）— 附帶在 R602 修復時一起改

---

## Round 51 (2026-03-04) — Reviewer 風險點轉最小可重現測試（tests-only）

### 前置說明

- 依指示先讀：
  - `.cursor/plans/PLAN.md`
  - `.cursor/plans/STATUS.md`
  - `DECISIONS.md`（**檔案不存在**）
- 實際採用 `.cursor/plans/DECISION_LOG.md` 作為決策檔來源（內容含 DEC-018 / DEC-019）。

### 本輪修改檔案（僅 tests）

- `tests/test_review_risks_round180.py`（新增）

### 測試覆蓋（對應 Reviewer 風險點）

| 風險 | 測試名稱 | 類型 | 目前狀態 |
|---|---|---|---|
| R600 | `test_month_end_dates_partial_month_returns_empty_list` | runtime 最小重現（空月結清單） | `pass` |
| R600 | `test_ensure_profile_should_have_explicit_empty_snapshot_dates_fallback` | source guard（要求空清單 fallback） | `expectedFailure` |
| R601 | `test_schema_hash_should_include_schedule_tag_in_reader` | source guard（reader hash 要含 schedule tag） | `expectedFailure` |
| R601 | `test_schema_hash_should_include_schedule_tag_in_writer` | source guard（writer hash 要含 schedule tag） | `expectedFailure` |
| R602 | `test_backfill_month_end_without_whitelist_should_not_preload` | runtime 最小重現（月結 + 無 whitelist 不應 preload） | `expectedFailure` |
| R603 | `test_backfill_preload_log_should_be_schedule_aware` | source guard（log 不應誤寫 fast-mode） | `expectedFailure` |
| R605 | `test_backfill_snapshot_dates_processes_only_filtered_sorted_dates` | runtime guard（交互行為正確） | `pass` |

### 執行方式

```bash
python -m unittest tests.test_review_risks_round180 -v
```

### 執行結果

```text
Ran 7 tests in 0.022s
OK (expected failures=5)
```

### 解讀

- 本輪目的為「把風險顯性化」，不是修 production。
- `expectedFailure=5` 代表 R600 fallback / R601 schedule-hash / R602 preload / R603 log wording 這些 reviewer 指出的問題已被測試鎖定，後續修 code 後可逐條移除 `expectedFailure`。
- R605（`--sample-rated` + 月結路徑）目前行為測試為綠燈。

### 下一步建議

1. 先修 R602（OOM 風險最高），修後移除對應 `expectedFailure`。
2. 再修 R600（空清單 fallback）與 R603（log），確保行為與可觀測性一致。
3. 若決定處理 cache 隔離，再修 R601（schema hash 加 schedule tag）。


---

## Round 52 (2026-03-04) — R600/R601/R602/R603 全部修完，所有 tests 轉綠

### 目標
上一輪的 5 個 `@expectedFailure` 測試代表尚未修的 Reviewer 風險。  
本輪把實作補齊，讓 7/7 tests 全部 `ok`（無 expectedFailure）。

### 測試結果

```
Ran 7 tests in 0.009s
OK
```

- 前一輪：`OK (expected failures=5)` → 本輪：`OK`（7 個 `ok`，0 個 expectedFailure）
- `@unittest.expectedFailure` 裝飾器僅在對應 bug 仍未修時才正確；修後裝飾器本身變成「測試寫錯」，故一併移除。

### 修改檔案與內容

#### `trainer/etl_player_profile.py`

| 風險 | 改動 |
|------|------|
| R601 writer | `_write_to_local_parquet` → **`_persist_local_parquet`**；加 `sched_tag: str = "_daily"` 參數；在 hash 計算中加入 `_sched_tag = sched_tag` |
| R601 傳遞鏈 | `build_player_profile_daily` 加 `sched_tag` 參數並轉傳給 `_persist_local_parquet` |
| R601/R602 | `backfill` 計算 `_sched_tag = "_month_end" if snapshot_dates is not None else "_daily"`；傳給兩處 `build_player_profile_daily` |
| R602 | **移除** preload 觸發條件中的 `or snapshot_dates is not None`——月結模式每年 ~12 筆，pushdown 讀即可，不應全表 preload |
| R603 | preload log 改為 schedule-aware：`f"DEC-019 month-end ({len(snapshot_dates)} dates)"` vs `f"fast-mode (interval={snapshot_interval_days} days)"` |

#### `trainer/trainer.py`

| 風險 | 改動 |
|------|------|
| R601 reader | `ensure_player_profile_daily_ready` schema hash 加 `_sched_tag = "_month_end" if (use_month_end_snapshots and not fast_mode) else "_daily"`；與 writer 公式對齊 |
| R600 | `_snap_dates` 計算後立刻檢查 `len(_snap_dates) == 0`；若空則 warning + fallback to `_snap_dates = None`（回歸 interval 路徑） |

#### `tests/test_review_risks_round180.py`

- 移除 5 個 `@unittest.expectedFailure` 裝飾器（R600、R601×2、R602、R603）——理由：對應 bug 已修，裝飾器本身已失效。

### 手動驗證方式

```bash
# 1. 快速測試 (2-3 秒)
python -m unittest tests.test_review_risks_round180 -v

# 2. Smoke：月結模式（不應 preload）
python -m trainer.trainer \
  --fast-mode --recent-chunks 1 --use-local-parquet --sample-rated 100

# 3. Smoke：月結模式 + schema hash 隔離（刪 .schema_hash 後確認重建）
rm data/player_profile_daily.schema_hash
python -m trainer.trainer \
  --fast-mode --recent-chunks 1 --use-local-parquet --sample-rated 100
```

### schema hash 向後相容注意事項

R601 reader 公式從 `md5(base+pop+horizon)` 改為 `md5(base+pop+horizon+sched_tag)`。  
這表示現有的 `player_profile_daily.parquet` cache（以 `_daily` 預設值寫入）與新讀取公式的 hash 會**不匹配**，觸發自動 rebuild。  
**屬預期行為**：避免月結/每日 cache 混用。若要保留現有 cache，先刪除 `.schema_hash` 讓下次 run 重新計算。

### 下一步建議

1. 執行 Smoke（上方步驟 2/3）確認 full-mode 不會 OOM
2. 跑一次完整 full run（不加 `--fast-mode`）計時，驗證 DEC-019 月結模式真的縮短 ETL 時間
3. （選項）把 `import calendar`（在 `_month_end_dates` 內）移到 `trainer.py` 檔案頂部（R604 cosmetic）

