# Feature Serving Incident - 2026-05-19

本文件記錄 `trainer_hightier` high-tier model 在 production serving 中發現的 feature supplyability 事故，並逐項對照目前程式已完成的緩解實作。

## Current Status

- 事故狀態：**核心根因已被修復與封鎖**（不再允許 silent null coverage 直接進 scoring）。
- 仍需持續監控：`source_mirror` 的資料覆蓋與更新穩定性（屬運維風險，不是原始設計缺陷）。

## Original Symptoms

- Production `prediction_log.features_json` 顯示 `fe__*` 大量為 null，`fe_features_missing = 18` 幾乎固定。
- `patron__*__w180d_m1snap` slow features 也大量為 null。
- Alert volume 與離線 `run_summary.json` 分佈嚴重偏離。

## Issue-by-Issue Closure

### Issue 1: `fe__*` 把 training bet-grain artifact 當 production supplier

**問題（當時）**
- `join_fe_derived_snapshot()` 以 `bet_id` merge training 產出的 `fe_derived_features.parquet`。
- live production bet IDs 不在 training artifact，造成系統性 miss。

**已實作緩解**
- 新增 production 主路徑：`join_production_fe_suppliers()`，拆分 short-term 與 mid-term supplier。
  - short-term：`fe_short_term_parquet`（bet-grain）。
  - mid-term：`mid_term_snapshot_parquet`（`canonical_id + anchor_gaming_day` ASOF）。
- scorer 優先走 production supplier join，並搭配 snapshot validation / freshness gate。

**程式位置**
- `trainer_hightier/serving/feature_builder.py`
  - `join_production_fe_suppliers(...)`
- `trainer_hightier/serving/scorer.py`
  - 主流程呼叫 `join_production_fe_suppliers(...)`
  - `validate_mid_term_artifact(...)`

### Issue 2: slow 180d 以 bet merge 導致 live 無法命中

**問題（當時）**
- slow artifact 可走 bet-grain merge，仍依賴舊 bet id。

**已實作緩解**
- slow supplier 以 canonical ASOF 為 production 預設與契約：
  - `slow_patron_grain=canonical_asof` 時，強制 schema 需有 `canonical_id + anchor_gaming_day`。
  - `join_slow_patron_snapshot()` 走 canonical ASOF 路徑，非 bet merge。
- slow monthly anchor 語意修正為 last full month data relative to today (not gaming day)。For example if today is May 10th, the snapshot must be computed using data of last month's end (Apr 30th) and backward; the snapshot is expected to be scheduled on May 1st so it covers full Apr data。

**程式位置**
- `trainer_hightier/serving/feature_builder.py`
  - `_slow_parquet_join_mode(...)`
  - `_join_slow_patron_canonical_asof_snapshot(...)`
  - `join_slow_patron_snapshot(...)`
- `trainer_hightier/utils/slow_patron_180d_monthly.py`
  - monthly anchor is last full month data relative to today

### Issue 3: 缺 production refresh job 與一致 source contract

**問題（當時）**
- 缺 deploy-managed refresh contract，容易退回使用 training artifact。

**已實作緩解**
- 建立 deploy-managed refresh supervisor（`deploy.main`）：
  - startup 僅同步修 hard failure（missing / invalid / hard-cap）。
  - hard failure refresh 失敗 => scorer-capable deploy fail-fast。
  - `stale_allowed` 交由背景 supervisor retry。
- refresh 前強制驗證 production mirror：
  - `source_mirror/cleaned_bet/`
  - `source_mirror/cleaned_session.parquet`

**程式位置**
- `trainer_hightier/deploy/main.py`
  - `_startup_snapshot_repair_or_raise(...)`
  - `_refresh_supervisor_once(...)`
- `trainer_hightier/serving/snapshot_updater.py`
  - `ensure_production_mirrors_ready(...)` 呼叫點
- `trainer_hightier/serving/production_source_mirror.py`
  - mirror schema/coverage 驗證

### Issue 4: stale / invalid snapshot 未被硬性擋下，可能持續污染 scoring

**問題（當時）**
- 缺乏完整 runtime gate，容易讓 null family 持續進模型。

**已實作緩解**
- Snapshot gate：
  - `build_scoring_snapshot_gate(...)` 阻擋 missing / invalid_grain / hard_cap_breached。
- Post-join smoke：
  - `post_join_feature_smoke(...)` 檢查 `fe__*` 或 `patron__*` 全 null，失敗即 raise。
- 狀態可觀測：
  - freshness / degraded 寫入 state 與 prediction log。

**程式位置**
- `trainer_hightier/serving/scorer.py`
  - `build_scoring_snapshot_gate(...)`
  - `post_join_feature_smoke(...)`
  - prediction log metadata 寫入
- `trainer_hightier/serving/snapshot_freshness.py`
  - freshness/validation 與 `build_deploy_startup_snapshot_plan(...)`

### Issue 5: 無法快速判斷 refresh 是否在修復、mirror 是否健康

**問題（當時）**
- 運維端缺少 supervisor 與 mirror 狀態 key。

**已實作緩解**
- 新增 `feature_state_meta` 監控鍵：
  - `refresh_supervisor_last_check_iso`
  - `mid_term_refresh_last_attempt_iso`
  - `slow_refresh_last_attempt_iso`
  - `slow_refresh_last_check_day`
  - `source_mirror_bet_status`
  - `source_mirror_session_status`

**程式位置**
- `trainer_hightier/serving/contracts.py`
- `trainer_hightier/serving/feature_state_store.py`
- `trainer_hightier/deploy/main.py`（寫入上述 meta）

## What This Incident Is No Longer

- 不再是「系統性 null features 仍可無聲過關」的狀態。
- 現在若供應出問題，預期行為是：
  - deploy fail-fast（hard failure），或
  - scorer gate/smoke 明確報錯，或
  - degraded 狀態可見且可追蹤。

## Remaining Operational Risk

- 若 `source_mirror` 未被正確 seed/刷新，refresh 會失敗。  
  這是顯性故障（有錯誤訊息與 meta 狀態），不是 silent skew。

## Verification Checklist (Current)

- `prediction_log` 抽樣檢查：
  - `fe_features_missing` 不再長時間固定為全缺失。
  - `patron__theo_win_sum__w180d_m1snap`、`patron__gaming_days_cnt__w180d_m1snap`、`patron__adt__w180d_m1snap` 非系統性全 null。
- `feature_state_meta`：
  - supervisor check/attempt key 持續更新。
  - `source_mirror_*_status` 為 valid 或可解釋錯誤訊息。
- `active_manifest.json`：
  - `slow_patron_grain=canonical_asof`
  - `mid_term_snapshot_parquet` 與 `slow_anchor_gaming_day_max` 持續前進。

## Operational Guidance

- 不要把 missing features 以 zero/median 靜默補值；維持 fail-fast/degraded 可見性。
- 沒通過 supplyability / freshness / smoke 的 bundle，不應 promotion。

